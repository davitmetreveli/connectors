import asyncio
import os
import re
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from functools import partial, wraps

import aiofiles
import aiohttp
import fastjsonschema
import msal
from aiofiles.tempfile import NamedTemporaryFile
from aiohttp.client_exceptions import ClientResponseError, ServerDisconnectedError
from fastjsonschema import JsonSchemaValueException

from connectors.filtering.validation import (
    AdvancedRulesValidator,
    SyncRuleValidationResult,
)
from connectors.logger import logger
from connectors.source import BaseDataSource
from connectors.utils import CacheWithTimeout, convert_to_b64, html_to_text, url_encode

if "OVERRIDE_URL" in os.environ:
    logger.warning("x" * 50)
    logger.warning(
        f"SHAREPOINT ONLINE CONNECTOR CALLS ARE REDIRECTED TO {os.environ['OVERRIDE_URL']}"
    )
    logger.warning("IT'S SUPPOSED TO BE USED ONLY FOR TESTING")
    logger.warning("x" * 50)
    override_url = os.environ["OVERRIDE_URL"]
    GRAPH_API_URL = override_url
    GRAPH_API_AUTH_URL = override_url
    REST_API_AUTH_URL = override_url
else:
    GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
    GRAPH_API_AUTH_URL = "https://login.microsoftonline.com"
    REST_API_AUTH_URL = "https://accounts.accesscontrol.windows.net"

DEFAULT_RETRY_SECONDS = 30
FILE_WRITE_CHUNK_SIZE = 1024
MAX_DOCUMENT_SIZE = 10485760
WILDCARD = "*"


class NotFound(Exception):
    pass


class MicrosoftSecurityToken:
    """Abstract token for connecting to one of Microsoft Azure services.

    This class is an abstract base class for getting auth token.

    It takes care of caching the token and asking for new token once the
    token expires.

    Classes that inherit from this class need to implement `async def _fetch_token(self)` method
    that needs to return a tuple: access_token<str> and expires_in<int>.

    To read more about tenants and authentication, see:
        - https://learn.microsoft.com/en-us/azure/active-directory/develop/quickstart-create-new-tenant
        - https://learn.microsoft.com/en-us/azure/active-directory/develop/quickstart-register-app"""

    def __init__(self, http_session, tenant_id, tenant_name, client_id, client_secret):
        """Initializer.

        Args:
            http_session (aiohttp.ClientSession): HTTP Client Session
            tenant_id (str): Azure AD Tenant Id
            tenant_name (str): Azure AD Tenant Name
            client_id (str): Azure App Client Id
            client_secret (str): Azure App Client Secret Value"""

        self._http_session = http_session
        self._tenant_id = tenant_id
        self._tenant_name = tenant_name
        self._client_id = client_id
        self._client_secret = client_secret

        self._token_cache = CacheWithTimeout()

    async def get(self):
        """Get bearer token for provided credentials.

        If token has been retrieved, it'll be taken from the cache.
        Otherwise, call to `_fetch_token` is made to fetch the token
        from 3rd-party service.

        Returns:
            str: bearer token for one of Microsoft services"""

        cached_value = self._token_cache.get()

        if cached_value:
            return cached_value

        now = (
            datetime.now()
        )  # We measure now before request to be on a pessimistic side
        try:
            access_token, expires_in = await self._fetch_token()
        except ClientResponseError as e:
            # Both Graph API and REST API return error codes that indicate different problems happening when authenticating.
            # Error Code serves as a good starting point classifying these errors, see the messages below:
            match e.status:
                case 400:
                    raise Exception(
                        "Failed to authorize to Sharepoint REST API. Please verify, that provided Tenant Id, Tenant Name and Client ID are valid."
                    ) from e
                case 401:
                    raise Exception(
                        "Failed to authorize to Sharepoint REST API. Please verify, that provided Secret Value is valid."
                    ) from e
                case _:
                    raise Exception(
                        f"Failed to authorize to Sharepoint REST API. Response Status: {e.status}, Message: {e.message}"
                    ) from e

        self._token_cache.set(access_token, now + timedelta(expires_in))

        return access_token

    async def _fetch_token(self):
        """Fetch token from Microsoft service.

        This method needs to be implemented in the class that inherits MicrosoftSecurityToken.

        Returns:
            (str, int) - a tuple containing access token as a string and number of seconds it will be valid for as an integer"""

        raise NotImplementedError


class GraphAPIToken(MicrosoftSecurityToken):
    """Token to connect to Microsoft Graph API endpoints."""

    async def _fetch_token(self):
        """Fetch API token for usage with Graph API

        Returns:
            (str, int) - a tuple containing access token as a string and number of seconds it will be valid for as an integer"""

        url = f"{GRAPH_API_AUTH_URL}/{self._tenant_id}/oauth2/v2.0/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = f"client_id={self._client_id}&scope=https://graph.microsoft.com/.default&client_secret={self._client_secret}&grant_type=client_credentials"

        async with self._http_session.post(url, headers=headers, data=data) as resp:
            json_response = await resp.json()
            access_token = json_response["access_token"]
            expires_in = int(json_response["expires_in"])
            return access_token, expires_in


class SharepointRestAPIToken(MicrosoftSecurityToken):
    """Token to connect to Sharepoint REST API endpoints."""

    async def _fetch_token(self):
        """Fetch API token for usage with Sharepoint REST API

        Returns:
            (str, int) - a tuple containing access token as a string and number of seconds it will be valid for as an integer"""

        url = f"{REST_API_AUTH_URL}/{self._tenant_id}/tokens/OAuth/2"
        # GUID in resource is always a constant used to create access token
        data = {
            "grant_type": "client_credentials",
            "resource": f"00000003-0000-0ff1-ce00-000000000000/{self._tenant_name}.sharepoint.com@{self._tenant_id}",
            "client_id": f"{self._client_id}@{self._tenant_id}",
            "client_secret": self._client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with self._http_session.post(url, headers=headers, data=data) as resp:
            json_response = await resp.json()
            access_token = json_response["access_token"]
            expires_in = int(json_response["expires_in"])

            return access_token, expires_in


class PermissionsMissing(Exception):
    """Exception class to notify that specific Application Permission is missing for the credentials used.
    See: https://learn.microsoft.com/en-us/graph/permissions-reference
    """
    pass


class MicrosoftAPISession:
    def __init__(self, http_session, api_token, scroll_field):
        self._http_session = http_session
        self._api_token = api_token
        self._semaphore = asyncio.Semaphore(
            10
        )  # TODO: make configurable, that's a scary property

        # Graph API and Sharepoint API scroll over slightly different fields:
        # - odata.nextPage for Sharepoint REST API uses
        # - @odata.nextPage for Graph API uses - notice the @ glyph
        # Therefore for flexibility I made it a field passed in the initializer,
        # but this abstraction can be better.
        self._scroll_field = scroll_field

    async def fetch(self, url, debug=False):
        return await self._get_json(url)

    async def pipe(self, url, stream):
        async with self._call_api(url) as resp:
            async for data in resp.content.iter_chunked(FILE_WRITE_CHUNK_SIZE):
                await stream.write(data)

    async def scroll(self, url):
        scroll_url = url

        while True:
            graph_data = await self._get_json(scroll_url)
            # We're yielding the whole page here, not one item
            yield graph_data["value"]

            if self._scroll_field in graph_data:
                scroll_url = graph_data[self._scroll_field]
            else:
                break

    async def _get_json(self, absolute_url):
        async with self._call_api(absolute_url) as resp:
            return await resp.json()

    @asynccontextmanager
    async def _call_api(self, absolute_url):
        while True:  # TODO: do 3 retries
            try:
                # Sharepoint / Graph API has quite strict throttling policies
                # If connector is overzealous, it can be banned for not respecting throttling policies
                # However if connector has a low setting for the semaphore, then it'll just be slow.
                # Change the value at your own risk
                await self._semaphore.acquire()

                token = await self._api_token.get()
                headers = {"authorization": f"Bearer {token}"}
                logger.debug(f"Calling Sharepoint Endpoint: {absolute_url}")

                async with self._http_session.get(
                    absolute_url,
                    headers=headers,
                ) as resp:
                    yield resp
                    return
            except ClientResponseError as e:
                if e.status == 429 or e.status == 503:
                    response_headers = e.headers or {}
                    retry_seconds = None
                    if "Retry-After" in response_headers:
                        retry_seconds = int(response_headers["Retry-After"])
                    else:
                        logger.warning(
                            "Response Code from Sharepoint Server is 429 but Retry-After header is not found, using default retry time: {DEFAULT_RETRY_SECONDS} seconds"
                        )
                        retry_seconds = DEFAULT_RETRY_SECONDS
                    logger.debug(
                        f"Rate Limited by Sharepoint: retry in {retry_seconds} seconds"
                    )

                    await asyncio.sleep(retry_seconds)  # TODO: use CancellableSleeps
                elif (
                    e.status == 403 or e.status == 401
                ):  # Might work weird, but Graph returns 403 and REST returns 401
                    raise PermissionsMissing(
                        f"Received Unauthorized response for {absolute_url}.\nVerify that Graph API [Sites.Read.All, Files.Read All] and Sharepoint [Sites.Read.All] permissions are granted to the app and admin consent is given. If the permissions and consent are correct, wait for several minutes and try again."
                    ) from e
                elif e.status == 404:
                    raise NotFound from e  # We wanna catch it in the code that uses this and ignore in some cases
                else:
                    raise
            finally:
                self._semaphore.release()


class SharepointOnlineClient:
    def __init__(self, tenant_id, tenant_name, client_id, client_secret):
        self._http_session = aiohttp.ClientSession(  # TODO: lazy create this
            headers={
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=None),
            raise_for_status=True,
        )

        self._tenant_id = tenant_id
        self._tenant_name = tenant_name
        self._tenant_name_pattern = re.compile(
            "https://(.*).sharepoint.com"
        )  # Used later for url validation

        self.graph_api_token = GraphAPIToken(
            self._http_session, tenant_id, tenant_name, client_id, client_secret
        )
        self.rest_api_token = SharepointRestAPIToken(
            self._http_session, tenant_id, tenant_name, client_id, client_secret
        )

        self._graph_api_client = MicrosoftAPISession(
            self._http_session, self.graph_api_token, "@odata.nextLink"
        )
        self._rest_api_client = MicrosoftAPISession(
            self._http_session, self.rest_api_token, "odata.nextLink"
        )

    async def site_collections(self):
        filter_ = url_encode("siteCollection/root ne null")
        select = "siteCollection,webUrl"

        async for page in self._graph_api_client.scroll(
            f"{GRAPH_API_URL}/sites/?$filter={filter_}&$select={select}"
        ):
            for site_collection in page:
                yield site_collection

    async def sites(self, parent_site_id, allowed_root_sites):
        select = ""

        async for page in self._graph_api_client.scroll(
            f"{GRAPH_API_URL}/sites/{parent_site_id}/sites?search=*&$select={select}"
        ):
            for site in page:
                # Filter out site collections that are not needed
                if (
                    WILDCARD not in allowed_root_sites
                    and site["name"] not in allowed_root_sites
                ):
                    continue

                yield site

    async def site_drives(self, site_id):
        select = ""

        async for page in self._graph_api_client.scroll(
            f"{GRAPH_API_URL}/sites/{site_id}/drives?$select={select}"
        ):
            for site_drive in page:
                yield site_drive

    async def drive_items(self, drive_id):
        select = ""

        directory_stack = []

        root = await self._graph_api_client.fetch(
            f"{GRAPH_API_URL}/drives/{drive_id}/root?$select={select}"
        )

        directory_stack.append(root["id"])
        yield root

        while len(directory_stack):
            folder_id = directory_stack.pop()

            async for page in self._graph_api_client.scroll(
                f"{GRAPH_API_URL}/drives/{drive_id}/items/{folder_id}/children?$select={select}"
            ):
                for drive_item in page:
                    if "folder" in drive_item:
                        directory_stack.append(drive_item["id"])
                    yield drive_item

    async def download_drive_item(self, drive_id, item_id, async_buffer):
        await self._graph_api_client.pipe(
            f"{GRAPH_API_URL}/drives/{drive_id}/items/{item_id}/content", async_buffer
        )

    async def site_lists(self, site_id):
        select = ""

        async for page in self._graph_api_client.scroll(
            f"{GRAPH_API_URL}/sites/{site_id}/lists?$select={select}"
        ):
            for site_list in page:
                yield site_list

    async def site_list_items(self, site_id, list_id):
        select = ""
        expand = "fields"

        async for page in self._graph_api_client.scroll(
            f"{GRAPH_API_URL}/sites/{site_id}/lists/{list_id}/items?$select={select}&$expand={expand}"
        ):
            for site_list in page:
                yield site_list

    async def site_list_item_attachments(self, site_web_url, list_title, list_item_id):
        self._validate_sharepoint_rest_url(site_web_url)

        url = f"{site_web_url}/_api/lists/GetByTitle('{list_title}')/items({list_item_id})?$expand=AttachmentFiles"

        try:
            list_item = await self._rest_api_client.fetch(url, debug=True)

            for attachment in list_item["AttachmentFiles"]:
                yield attachment
        except NotFound:
            # We can safely ignore cause Sharepoint can return 404 in case List Item is of specific types that do not support/have attachments
            # Yes, makes no sense to me either.
            return

    async def download_attachment(self, attachment_absolute_path, async_buffer):
        self._validate_sharepoint_rest_url(attachment_absolute_path)

        await self._rest_api_client.pipe(
            f"{attachment_absolute_path}/$value", async_buffer
        )

    async def site_pages(self, site_web_url):
        self._validate_sharepoint_rest_url(site_web_url)

        select = ""
        url = f"{site_web_url}/_api/web/lists/GetByTitle('Site%20Pages')/items?$select={select}"

        async for page in self._rest_api_client.scroll(url):
            for site_page in page:
                yield site_page

    def _validate_sharepoint_rest_url(self, url):
        if "OVERRIDE_URL" in os.environ:  # TODO: make it in a call instead of this
            return

        # I haven't found a better way to validate tenant name for now.
        actual_tenant_name = self._tenant_name_pattern.findall(url)[0]

        if self._tenant_name != actual_tenant_name:
            raise Exception(
                f"Unable to call Sharepoint REST API - tenant name is invalid. Authenticated for tenant name: {self._tenant_name}, actual tenant name for the service: {actual_tenant_name}."
            )

    async def close(self):
        await self._http_session.close()


class SharepointOnlineAdvancedRulesValidator(AdvancedRulesValidator):
    """
    Validate advanced rules for MongoDB, so that they're adhering to the motor asyncio API (see: https://motor.readthedocs.io/en/stable/api-asyncio/asyncio_motor_collection.html)
    """

    SCHEMA_DEFINITION = {
        "type": "object",
        "properties": {
            "maxDataAge": {"type": "integer"},
        },
        "additionalProperties": False,
    }

    SCHEMA = fastjsonschema.compile(definition=SCHEMA_DEFINITION)

    async def validate(self, advanced_rules):
        try:
            SharepointOnlineAdvancedRulesValidator.SCHEMA(advanced_rules)

            return SyncRuleValidationResult.valid_result(
                rule_id=SyncRuleValidationResult.ADVANCED_RULES
            )
        except JsonSchemaValueException as e:
            return SyncRuleValidationResult(
                rule_id=SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=f"{e.message}. Make sure advanced filtering rules follow the following schema: {SharepointOnlineAdvancedRulesValidator.SCHEMA_DEFINITION['properties']}",
            )


class SharepointOnlineDataSource(BaseDataSource):
    """Sharepoint Online"""

    name = "Sharepoint Online"
    service_type = "sharepoint_online"

    def __init__(self, configuration):
        super().__init__(configuration=configuration)

        self._client = None

    @property
    def client(self):
        if not self._client:
            tenant_id = self.configuration["tenant_id"]
            tenant_name = self.configuration["tenant_name"]
            client_id = self.configuration["client_id"]
            client_secret = self.configuration["secret_value"]

            self._client = SharepointOnlineClient(
                tenant_id, tenant_name, client_id, client_secret
            )

        return self._client

    @classmethod
    def get_default_configuration(cls):
        return {
            "tenant_id": {
                "label": "Tenant Id",
                "order": 1,
                "type": "str",
                "value": "",
            },
            "tenant_name": {  # TODO: when Tenant API is going out of Beta, we can remove this field
                "label": "Tenant Name",
                "order": 2,
                "type": "str",
                "value": "",
            },
            "client_id": {
                "label": "Client Id",
                "order": 3,
                "type": "str",
                "value": "",
            },
            "secret_value": {
                "label": "Secret Value",
                "order": 4,
                "sensitive": True,
                "type": "str",
                "value": "",
            },
            "site_collections": {
                "display": "textarea",
                "label": "Comma-separated list of sites",
                "tooltip": "A comma-separated list of sites to ingest data from. Use * to include all available sites.",
                "order": 5,
                "type": "list",
                "value": "",
            },
        }

    async def validate_config(self):
        # Check that we can log in into Graph API
        await self.client.graph_api_token.get()

        # Check that we can log in into Sharepoint REST API
        await self.client.rest_api_token.get()


        # TODO: check that tenant_name is valid

        # Check that we at least have permissions to fetch sites and actual site names are correct
        configured_root_sites = self.configuration["site_collections"]

        remote_sites = []

        async for site_collection in self.client.site_collections():
            async for site in self.client.sites(
                site_collection["siteCollection"]["hostname"], [WILDCARD]
            ):
                remote_sites.append(site["name"])

        if WILDCARD in configured_root_sites:
            return

        intersection = [
            value for value in remote_sites if value in configured_root_sites
        ]

        missing = [x for x in configured_root_sites if x not in remote_sites]

        truncated_available_sites = remote_sites[:10]

        if missing:
            raise Exception(
                f"The specified SharePoint sites [{', '.join(missing)}] could not be retrieved during sync. Review your configuration settings of Microsoft SharePoint access with your SharePoint administrator."
            )

    async def get_docs(self, filtering=None):
        max_data_age = None

        if filtering is not None and filtering.has_advanced_rules():
            advanced_rules = filtering.get_advanced_rules()
            max_data_age = advanced_rules["maxDataAge"]

        async for site_collection in self.client.site_collections():
            site_collection["_id"] = site_collection["webUrl"]
            site_collection["object_type"] = "site_collection"
            yield site_collection, None

            async for site in self.client.sites(
                site_collection["siteCollection"]["hostname"],
                self.configuration["site_collections"],
            ):  # TODO: simplify and eliminate root call
                site["_id"] = site["id"]
                site["object_type"] = "site"

                yield site, None

                async for site_drive in self.client.site_drives(site["id"]):
                    site_drive["_id"] = site_drive["id"]
                    site_drive["object_type"] = "site_drive"
                    yield site_drive, None

                    async for drive_item in self.client.drive_items(site_drive["id"]):
                        drive_item["_id"] = drive_item["id"]
                        drive_item["object_type"] = "drive_item"
                        drive_item["_timestamp"] = drive_item["lastModifiedDateTime"]

                        download_func = None

                        if "@microsoft.graph.downloadUrl" in drive_item:
                            modified_date = datetime.strptime(
                                drive_item["lastModifiedDateTime"], "%Y-%m-%dT%H:%M:%SZ"
                            )
                            if (
                                max_data_age
                                and modified_date
                                < datetime.now() - timedelta(max_data_age)
                            ):
                                logger.warning(
                                    f"Not downloading file {drive_item['name']}: last modified on {drive_item['lastModifiedDateTime']}"
                                )
                            elif drive_item["size"] > MAX_DOCUMENT_SIZE:
                                logger.warning(
                                    f"Not downloading file {drive_item['name']} of size {drive_item['size']}"
                                )
                            else:
                                download_func = partial(self.get_content, drive_item)

                        yield drive_item, download_func

                async for site_list in self.client.site_lists(site["id"]):
                    site_list["_id"] = site_list["id"]
                    site_list["object_type"] = "site_list"

                    yield site_list, None

                    async for list_item in self.client.site_list_items(
                        site["id"], site_list["id"]
                    ):
                        list_item["_id"] = list_item["id"]
                        list_item["object_type"] = "list_item"
                        content_type = list_item["contentType"]["name"]

                        if content_type in [
                            "Web Template Extensions",
                            "Client Side Component Manifests",
                        ]:  # TODO: make it more flexible. For now I ignore them cause they 404 all the time
                            continue

                        if "Attachments" in list_item["fields"]:
                            async for list_item_attachment in self.client.site_list_item_attachments(
                                site["webUrl"], site_list["name"], list_item["id"]
                            ):
                                list_item_attachment["_id"] = list_item_attachment[
                                    "odata.id"
                                ]
                                list_item_attachment[
                                    "object_type"
                                ] = "list_item_attachment"
                                list_item_attachment["_timestamp"] = list_item[
                                    "lastModifiedDateTime"
                                ]
                                attachment_download_func = partial(
                                    self.get_attachment, list_item_attachment
                                )
                                yield list_item_attachment, attachment_download_func

                        download_func = None

                        yield list_item, download_func

                async for site_page in self.client.site_pages(site["webUrl"]):
                    site_page["_id"] = site_page["GUID"]
                    site_page["object_type"] = "site_page"

                    for html_field in ["LayoutWebpartsContent", "CanvasContent1"]:
                        if html_field in site_page:
                            site_page[html_field] = html_to_text(site_page[html_field])

                    yield site_page, None

    async def get_attachment(self, attachment, timestamp=None, doit=False):
        if not doit:
            return

        result = {
            "_id": attachment["odata.id"],
            "_timestamp": datetime.now(),  # attachments cannot be modified in-place, so we can consider that object ids are permanent
        }

        source_file_name = ""
        async with NamedTemporaryFile(mode="wb", delete=False) as async_buffer:
            await self.client.download_attachment(attachment["odata.id"], async_buffer)

            source_file_name = async_buffer.name

        await asyncio.to_thread(
            convert_to_b64,
            source=source_file_name,
        )
        async with aiofiles.open(file=source_file_name, mode="r") as target_file:
            content = (await target_file.read()).strip()
            result["_attachment"] = content

        return result

    async def get_content(self, drive_item, timestamp=None, doit=False):
        document_size = int(drive_item["size"])

        if not (doit and document_size):
            return

        if document_size > MAX_DOCUMENT_SIZE:
            return

        result = {
            "_id": drive_item["id"],
            "_timestamp": drive_item["lastModifiedDateTime"],
        }

        source_file_name = ""
        async with NamedTemporaryFile(mode="wb", delete=False) as async_buffer:
            await self.client.download_drive_item(
                drive_item["parentReference"]["driveId"], drive_item["id"], async_buffer
            )

            source_file_name = async_buffer.name

        await asyncio.to_thread(
            convert_to_b64,
            source=source_file_name,
        )
        async with aiofiles.open(file=source_file_name, mode="r") as target_file:
            # base64 on macOS will add a EOL, so we strip() here
            content = (await target_file.read()).strip()
            result["_attachment"] = content

        return result

    async def ping(self):
        pass

    async def close(self):
        await self.client.close()

    def advanced_rules_validators(self):
        return [SharepointOnlineAdvancedRulesValidator()]
