"""Module providing a client for interacting with the DeLonghi dehumidifier API."""

import base64
from datetime import datetime
from enum import Enum
import json
import logging
import time
from typing import Final
import urllib.parse
import aiohttp

# API Docs: https://docs.aylanetworks.com/reference

# Thanks to https://github.com/duckwc/ECAMpy for the code to token conversion

SDK_BUILD = 16650

API_KEY = "3_e5qn7USZK-QtsIso1wCelqUKAK_IVEsYshRIssQ-X-k55haiZXmKWDHDRul2e5Y2"
CLIENT_ID = "1S8q1WJEs-emOB43Z0-66WnL"
CLIENT_SECRET = "lmnceiD0B-4KPNN5ZS6WuWU70j9V5BCuSlz2OPsvHkyLryhMkJkPvKsivfTq3RfNYj8GpCELtOBvhaDIzKcBtg"
AUTHORIZATION_HEADER = (
    "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
)
APP_ID = "DeLonghiComfort2-mw-id"
APP_SECRET = "DeLonghiComfort2-Yg4miiqiNcf0Or-EhJwRh7ACfBY"

BROWSER_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 13_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/79.0.3945.73 Mobile/15E148 Safari/604.1"
TOKEN_USER_AGENT = "DeLonghiComfort/3 CFNetwork/1568.300.101 Darwin/24.2.0"
API_USER_AGENT = "DeLonghiComfort/5.1.1 (iPhone; iOS 18.2; Scale/3.00)"

LANGUAGES = {
    "en": "GB",
    "pt": "PT",
    "en-ca": "CA",
    "fr-ca": "CA",
    "es-mx": "MX",
    "es-co": "CO",
    "es-pe": "PE",
    "en-us": "US",
    "pt-br": "BR",
    "es-cl": "CL",
    "en-za": "ZA",
    "es": "ES",
    "fr": "FR",
    "lu": "LU",
    "nl": "NL",
    "my": "MY",
    "fr-be": "BE",
    "nl-inf": "BE",
    "de": "DE",
    "fr-ch": "CH",
    "de-inf": "CH",
    "it": "IT",
    "mt-mt": "MT",
    "en-mt": "MT",
    "hr": "HR",
    "sr": "RS",
    "sl": "SI",
    "br": "BG",
    "el": "GR",
    "ro": "RO",
    "tr": "TR",
    "cs": "CZ",
    "sk": "SK",
    "hu": "HU",
    "de-at": "AT",
    "uk": "UA",
    "sv": "SE",
    "fi": "FI",
    "no": "NO",
    "da": "DK",
    "pl": "PL",
    "et-ee": "EE",
    "lt-lt": "LT",
    "lv-lv": "LV",
    "en-ae": "AE",
    "ar-ae": "AE",
    "en-sg": "SG",
    "en-my": "MY",
    "en-au": "AU",
    "en-nz": "NZ",
    "ja": "JP",
    "ko": "KR",
    "en-kh": "KH",
    "en-hk": "HK",
    "en-bd": "BD",
    "en-th": "TH",
    "th": "TH",
    "es-ar": "AR",
    "ar-eg": "EG",
    "en-eg": "EG",
    "en-in": "IN",
    "en-ir": "IR",
    "fa": "IR",
    "en-il": "IL",
    "en-sa": "SA",
    "ar-sa": "SA",
    "en-ie": "IE",
    "en-id": "ID",
    "en-ph": "PH",
    "zh-tw": "TW",
    "en-om": "OM",
    "en-qa": "QA",
    "en-bh": "BH",
    "en-kw": "KW",
    "vi": "VN",
}

LANGUAGE_COMMS_KEYS = {
    "CA": "profiledCommunicationCA",
    "MX": "profiledCommunicationMXCO",
    "CO": "profiledCommunicationMXCO",
    "US": "profiledCommunicationUS",
    "BR": "profiledCommunicationBR",
    "CL": "profiledCommunicationCL",
    "AR": "profiledCommunicationCL",
    "PE": "profiledCommunicationCL",
    "ZA": "profiledCommunicationZA",
    "PT": "profiledCommunicationPT",
    "ES": "profiledCommunicationES",
    "GB": "profiledCommunicationGB",
    "IE": "profiledCommunicationGB",
    "FR": "profiledCommunicationFR",
    "NL": "profiledCommunicationNL",
    "BE": "profiledCommunicationBE",
    "LU": "profiledCommunicationBE",
    "DE": "profiledCommunicationDE",
    "CH": "profiledCommunicationCH",
    "IT": "profiledCommunicationIT",
    "MT": "profiledCommunicationHRRSSIBG",
    "HR": "profiledCommunicationHRRSSIBG",
    "RS": "profiledCommunicationHRRSSIBG",
    "SI": "profiledCommunicationHRRSSIBG",
    "BG": "profiledCommunicationHRRSSIBG",
    "GR": "profiledCommunicationGR",
    "RO": "profiledCommunicationRO",
    "TR": "profiledCommunicationTR",
    "CZ": "profiledCommunicationCZSKHU",
    "SK": "profiledCommunicationCZSKHU",
    "HU": "profiledCommunicationCZSKHU",
    "AT": "profiledCommunicationAT",
    "UA": "profiledCommunicationUA",
    "SE": "profiledCommunicationSEFINODK",
    "FI": "profiledCommunicationSEFINODK",
    "NO": "profiledCommunicationSEFINODK",
    "DK": "profiledCommunicationSEFINODK",
    "PL": "profiledCommunicationPLEELTLV",
    "EE": "profiledCommunicationPLEELTLV",
    "LT": "profiledCommunicationPLEELTLV",
    "LV": "profiledCommunicationPLEELTLV",
    "AE": "profiledCommunicationAE",
    "EG": "profiledCommunicationAE",
    "IN": "profiledCommunicationAE",
    "IR": "profiledCommunicationAE",
    "IL": "profiledCommunicationAE",
    "SA": "profiledCommunicationAE",
    "OM": "profiledCommunicationAE",
    "QA": "profiledCommunicationAE",
    "BH": "profiledCommunicationAE",
    "KW": "profiledCommunicationAE",
    "SG": "profiledCommunicationSG",
    "MY": "profiledCommunicationMY",
    "AU": "profiledCommunicationAU",
    "NZ": "profiledCommunicationNZ",
    "JP": "profiledCommunicationJP",
    "KR": "profiledCommunicationKR",
    "KH": "profiledCommunicationHKBDKHTH",
    "HK": "profiledCommunicationHKBDKHTH",
    "BD": "profiledCommunicationHKBDKHTH",
    "TH": "profiledCommunicationHKBDKHTH",
    "ID": "profiledCommunicationHKBDKHTH",
    "PH": "profiledCommunicationHKBDKHTH",
    "TW": "profiledCommunicationHKBDKHTH",
    "VN": "profiledCommunicationHKBDKHTH",
}

LANGUAGE_COUNTRIES = {
    "en": "United Kingdom",
    "pt": "Portugal",
    "en-ca": "Canada",
    "fr-ca": "Canada",
    "es-mx": "Mexico",
    "es-co": "Colombia",
    "es-pe": "Peru",
    "en-us": "United States",
    "pt-br": "Brazil",
    "es-cl": "Chile",
    "en-za": "South Africa",
    "es": "Spain",
    "fr": "France",
    "lu": "Luxembourg",
    "nl": "Netherlands",
    "my": "Malaysia",
    "fr-be": "Belgium",
    "nl-inf": "Belgium",
    "de": "Germany",
    "fr-ch": "Switzerland",
    "de-inf": "Switzerland",
    "it": "Italy",
    "mt-mt": "Malta",
    "en-mt": "Malta",
    "hr": "Croatia",
    "sr": "Serbia",
    "sl": "Slovenia",
    "br": "Bulgaria",
    "el": "Greece",
    "ro": "Romania",
    "tr": "Turkey",
    "cs": "Czechia",
    "sk": "Slovakia",
    "hu": "Hungary",
    "de-at": "Austria",
    "uk": "Ukraine",
    "sv": "Sweden",
    "fi": "Finland",
    "no": "Norway",
    "da": "Denmark",
    "pl": "Poland",
    "et-ee": "Estonia",
    "lt-lt": "Lithuania",
    "lv-lv": "Latvia",
    "en-ae": "United Arab Emirates",
    "ar-ae": "United Arab Emirates",
    "en-sg": "Singapore",
    "en-my": "Malaysia",
    "en-au": "Australia",
    "en-nz": "New Zealand",
    "ja": "Japan",
    "ko": "South Korea",
    "en-kh": "Cambodia",
    "en-hk": "Hong Kong",
    "en-bd": "Bangladesh",
    "en-th": "Thailand",
    "th": "Thailand",
    "es-ar": "Argentina",
    "ar-eg": "Egypt",
    "en-eg": "Egypt",
    "en-in": "India",
    "en-ir": "Iran",
    "fa": "Iran",
    "en-il": "Israel",
    "en-sa": "Saudi Arabia",
    "ar-sa": "Saudi Arabia",
    "en-ie": "Ireland",
    "en-id": "Indonesia",
    "en-ph": "Philippines",
    "zh-tw": "Taiwan",
    "en-om": "Oman",
    "en-qa": "Qatar",
    "en-bh": "Bahrain",
    "en-kw": "Kuwait",
    "vi": "Vietnam",
}


class Status(Enum):
    """Enum representing the status of the DeLonghi dehumidifier.

    Attributes:
      ON (int): The dehumidifier is turned on.
      OFF (int): The dehumidifier is turned off.

    """

    ON = 1
    OFF = 2


STATUS_BY_VALUE: Final = {status.value: status for status in Status}


class Mode(Enum):
    """Enum representing the status of the DeLonghi dehumidifier.

    Attributes:
      ON (int): The dehumidifier is turned on.
      OFF (int): The dehumidifier is turned off.

    """

    DEHUMIDIFY = 1
    DRY_CLOTHES = 2
    PURIFIER = 3
    REAL_FEEL = 100


MODE_BY_NAME: Final = {mode.name: mode for mode in Mode}
MODE_BY_VALUE: Final = {mode.value: mode for mode in Mode}


class OffOnStatus(Enum):
    """Enum representing status of certain features (ie: eco and swing) of the DeLonghi dehumidifier.

    Attributes:
      OFF (int): The feature is turned off.
      ON (int): The feature is turned on.

    """

    OFF = 0
    ON = 1


OFF_ON_STATUS_BY_VALUE: Final = {status.value: status for status in OffOnStatus}


class FilterStatus(Enum):
    """Represents the status of a filter.

    Attributes:
        GOOD (int): The filter is in good condition.
        NEEDS_REPLACEMENT (int): The filter needs to be replaced.

    """

    GOOD = 1
    NEEDS_REPLACEMENT = 2


FILTER_STATUS_BY_VALUE: Final = {status.value: status for status in FilterStatus}

_LOGGER = logging.getLogger(__name__)


class APIClient:
    """Client for interacting with the DeLonghi dehumidifier API."""

    def __init__(
        self, session: aiohttp.ClientSession, language: str, email: str, password: str
    ) -> None:
        """Initialize."""
        self.language = language
        self.email = email
        self.password = password
        self.session = session
        self.refresh_token = None
        self.access_token = None
        self.token_expiry = time.time()
        self.device_properties = None
        self.device_properties_timestamp = 0.0
        self.device_dsn = None

    async def authenticate(self) -> bool:
        """Authenticate with the DeLonghi service.

        This tests the authentication process and retrieves an access token
        from the DeLonghi service. If the authentication is successful, it returns True.

        Returns:
          bool: True if authentication is successful.

        """
        access_token = await self.get_access_token()
        if access_token is None or access_token == "":
            return False
        return True

    async def get_product_name(self) -> str:
        """Retrieve the pretty product name defined by the user when setting up the device.

        This method fetches the properties of the product and returns the value
        associated with the "product_name" key from the first property in the list.

        Returns:
            str: The product name.

        """
        properties = await self.get_properties()
        return properties[0].get("product_name")

    async def get_appliance_model(self) -> str:
        """Retrieve the appliance model.

        Returns:
            str: The model of the appliance.

        """
        return await self.get_str_property("appliance_model")

    async def get_firmware_version(self) -> str:
        """Retrieve the firmware version of the device.

        Returns:
            str: The firmware version of the device.

        """
        return await self.get_str_property("firmware_version")

    async def get_hardware_version(self) -> str:
        """Retrieve the hardware version of the device.

        Returns:
            str: The hardware version of the device.

        """
        return await self.get_str_property("hardware_version")

    async def get_current_humidity(self) -> int:
        """Retrieve the current humidity property of the dehumidifier.

        Returns:
          int: The current humidity property value as an integer between 0 and 100.

        """
        current_humidity = await self.get_int_property("current_humidity")
        return int(current_humidity)

    async def get_humidity_setpoint(self) -> int:
        """Retrieve the humidity setpoint property of the dehumidifier.

        Returns:
          int: The humidity setpoint property value as an integer between 0 and 100.

        """
        humidity_setpoint = await self.get_int_property("humidity_setpoint")
        return int(humidity_setpoint)

    async def get_current_speed(self) -> int:
        """Retrieve the current speed property of the dehumidifier.

        Returns:
          int: The current speed property value as an integer.

        """
        current_speed = await self.get_int_property("current_speed")
        return int(current_speed)

    async def get_device_mode(self) -> Mode:
        """Retrieve the device mode property of the dehumidifier.

        Returns:
          int: The device mode property value as an integer.

        """
        device_mode = await self.get_int_property("device_mode")
        return MODE_BY_VALUE[device_mode]

    async def get_device_status(self) -> Status:
        """Retrieve the device status property of the dehumidifier.

        Returns:
          int: The device status property value as an integer.

        """
        device_status = await self.get_int_property("device_status")
        return STATUS_BY_VALUE[device_status]

    async def get_filter_change_alarm(self) -> OffOnStatus:
        """Retrieve the filter change alarm property of the dehumidifier.

        Returns:
          int: The filter change alarm property value as an integer.

        """
        filter_change_alarm = await self.get_int_property("filter_change_alarm")
        return OFF_ON_STATUS_BY_VALUE[filter_change_alarm]

    async def get_filter_life(self) -> int:
        """Retrieve the filter life property of the dehumidifier.

        Returns:
          int: The filter life property value as an integer.

        """
        return await self.get_int_property("filter_life")

    async def get_filter_status(self) -> FilterStatus:
        """Retrieve the filter status property of the dehumidifier.

        Returns:
          int: The filter status property value as an integer.

        """
        filter_status = await self.get_int_property("filter_status")
        return FILTER_STATUS_BY_VALUE[filter_status]

    async def get_heat_exchanger_temp(self) -> int:
        """Retrieve the heat exchanger temperature property of the dehumidifier.

        Returns:
          int: The heat exchanger temperature property value as an integer.

        """
        return await self.get_int_property("heat_exchanger_temp")

    async def get_room_temp(self) -> int:
        """Retrieve the room temperature property of the dehumidifier.

        Returns:
          int: The room temperature property value as an integer.

        """
        return await self.get_int_property("room_temp")

    async def get_rotation_speed(self) -> int:
        """Retrieve the rotation speed property of the dehumidifier.

        Returns:
          int: The rotation speed property value as an integer.

        """
        return await self.get_int_property("rotation_speed")

    async def get_swing(self) -> OffOnStatus:
        """Retrieve the swing property of the dehumidifier.

        Returns:
          int: The swing property value as an integer.

        """
        swing = await self.get_int_property("swing")
        return OFF_ON_STATUS_BY_VALUE[swing]

    async def get_eco(self) -> OffOnStatus:
        """Retrieve the eco property of the dehumidifier.

        Returns:
          int: The eco property value as an integer.

        """
        eco = await self.get_int_property("set_eco")
        return OFF_ON_STATUS_BY_VALUE[eco]

    async def set_status(self, status: Status) -> dict:
        """Set the operation status.

        - 1 On
        - 2 Off
        """
        device_dsn = await self.get_first_device()
        return await self.post_request(
            f"apiv1/dsns/{device_dsn}/properties/set_status/datapoints.json",
            {"datapoint": {"value": status.value}},
        )

    async def set_humidity(self, value: int) -> dict:
        """Set the target humidity level.

        - [0,100] %
        """
        device_dsn = await self.get_first_device()
        return await self.post_request(
            f"apiv1/dsns/{device_dsn}/properties/humidity_setpoint/datapoints.json",
            {"datapoint": {"value": value}},
        )

    async def set_mode(self, mode: Mode) -> dict:
        """Set the operation mode.

        - 1   Dehumidifier
        - 2   Dry Clothes
        - 3   Air purifier
        - 100 Real Feel
        """
        device_dsn = await self.get_first_device()

        if mode == Mode.REAL_FEEL:
            return await self.post_request(
                f"apiv1/dsns/{device_dsn}/properties/activate_realfeel/datapoints.json",
                {"datapoint": {"value": "AQIDChIXHEY8Mig="}},
            )

        return await self.post_request(
            f"apiv1/dsns/{device_dsn}/properties/device_mode/datapoints.json",
            {"datapoint": {"value": mode.value}},
        )

    async def set_swing(self, status: OffOnStatus) -> dict:
        """Set the swing operation mode.

        - 0 Off
        - 1 On
        """
        device_dsn = await self.get_first_device()
        return await self.post_request(
            f"apiv1/dsns/{device_dsn}/properties/swing/datapoints.json",
            {"datapoint": {"value": status.value}},
        )

    async def set_eco(self, status: OffOnStatus) -> dict:
        """Set the eco operation mode.

        - 0 Off
        - 1 On
        """
        device_dsn = await self.get_first_device()
        return await self.post_request(
            f"apiv1/dsns/{device_dsn}/properties/set_eco/datapoints.json",
            {"datapoint": {"value": status.value}},
        )

    async def get_access_token(self):
        """Retrieve the access token.

        If the access token is already available as an attribute, it returns the existing token.
        Otherwise, it fetches a new access token.

        Returns:
          str: The access token.

        """
        if (
            self.access_token is not None
            and self.access_token != ""
            and self.token_expiry > time.time()
        ):
            _LOGGER.debug("Using existing access token")
            return self.access_token

        return await self.get_new_access_token()

    async def get_new_access_token(self):
        """Retrieve a new access token using the refresh token if available.

        If no refresh token is available or the refresh token fails, it falls back to performing login and token acquisition.

        Returns:
          str: The new access token.

        """
        if self.refresh_token is None or self.refresh_token == "":
            # If no refresh token is available, perform login and token acquisition
            return await self.get_new_refresh_token()

        _LOGGER.debug("Getting new access token")
        # Attempt to use the refresh token to get a new access token
        url = "https://user-field-eu.aylanetworks.com/users/refresh_token.json"
        headers = {
            "User-Agent": TOKEN_USER_AGENT,
            "Content-Type": "application/json",
        }
        body = {"user": {"refresh_token": self.refresh_token}}
        response = await self.session.post(url, headers=headers, json=body)
        response = await response.json()

        if response.status != 200:
            _LOGGER.error(
                "Failed retrieving new access token with response: %s", response
            )
            _LOGGER.error("Falling back to login")
            return await self.get_new_refresh_token()

        data = await response.json()

        self.refresh_token = data["refresh_token"]
        self.access_token = data["access_token"]
        self.token_expiry = time.time() + int(data["expires_in"])

        _LOGGER.debug("data get_new_access_token: %s", data)

        return self.access_token

    async def get_new_refresh_token(self):
        """Obtain a new refresh token and access token.

        This method follows a multi-step process to authenticate with the Delonghi Dehumidifier API,
        retrieve user information, and obtain a new refresh token and access token.

        Steps:
        1. Start the authentication process by requesting an authorization code.
        2. Fetch Gigya session data.
        3. Perform login with user credentials.
        4. Retrieve user information.
        5. Provide user consent.
        6. Continue the authorization process to obtain an authorization code.
        7. Exchange the authorization code for an IDP access token.
        8. Exchange the IDP access token for an Ayla token.

        Returns:
          str: The new access token.

        """

        _LOGGER.debug("Getting new refresh token")

        # Step 1: Start authentication process
        url = f"https://fidm.eu1.gigya.com/oidc/op/v1.0/{API_KEY}/authorize"
        headers = {"User-Agent": BROWSER_USER_AGENT}
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": "https://google.it",
            "scope": "openid email profile UID comfort en alexa",
            "nonce": str(int(datetime.now().timestamp())),
        }
        response = await self.session.get(
            url, headers=headers, params=params, allow_redirects=False
        )
        location = response.headers.get("Location")
        context = await self.get_query_param(location, "context")

        # Step 2: Fetch Gigya session data
        url = "https://socialize.eu1.gigya.com/socialize.getIDs"
        headers = {"User-Agent": BROWSER_USER_AGENT}
        params = {
            "APIKey": API_KEY,
            "includeTicket": "true",
            "pageURL": "https://aylaopenid.delonghigroup.com/",
            "sdk": "js_latest",
            "sdkBuild": SDK_BUILD,
            "format": "json",
        }
        response = await self.session.get(url, headers=headers, params=params)
        response = await response.text()
        response = json.loads(response)

        ucid = response["ucid"]
        gmid = response["gmid"]
        gmid_ticket = response["gmidTicket"]

        # Step 3: Login
        risk_context_json = json.dumps(
            {
                "b0": 4494,
                "b1": [0, 2, 2, 0],
                "b2": 2,
                "b3": [],
                "b4": 2,
                "b5": 1,
                "b6": BROWSER_USER_AGENT,
                "b7": [
                    {
                        "name": "PDF Viewer",
                        "filename": "internal-pdf-viewer",
                        "length": 2,
                    },
                    {
                        "name": "Chrome PDF Viewer",
                        "filename": "internal-pdf-viewer",
                        "length": 2,
                    },
                    {
                        "name": "Chromium PDF Viewer",
                        "filename": "internal-pdf-viewer",
                        "length": 2,
                    },
                    {
                        "name": "Microsoft Edge PDF Viewer",
                        "filename": "internal-pdf-viewer",
                        "length": 2,
                    },
                    {
                        "name": "WebKit built-in PDF",
                        "filename": "internal-pdf-viewer",
                        "length": 2,
                    },
                ],
                "b8": datetime.now().strftime("%H:%M:%S"),
                "b9": 0,
                "b10": {"state": "denied"},
                "b11": False,
                "b13": [5, "440|956|24", False, True],
            }
        )

        url = "https://accounts.eu1.gigya.com/accounts.login"
        headers = {"User-Agent": BROWSER_USER_AGENT}
        data = {
            "loginID": self.email,
            "password": self.password,
            "sessionExpiration": 7884009,
            "targetEnv": "jssdk",
            "include": "profile,data,emails,subscriptions,preferences",
            "includeUserInfo": "true",
            "loginMode": "standard",
            "lang": self.language,
            "riskContext": await self.url_encode(risk_context_json),
            "APIKey": API_KEY,
            "source": "showScreenSet",
            "sdk": "js_latest",
            "authMode": "cookie",
            "pageURL": "https://aylaopenid.delonghigroup.com/",
            "gmid": gmid,
            "ucid": ucid,
            "sdkBuild": SDK_BUILD,
            "format": "json",
        }
        response = await self.session.post(url, headers=headers, data=data)
        response = await response.text()
        response = json.loads(response)

        login_token = response["sessionInfo"]["login_token"]

        # Step 4: Get user info
        url = "https://socialize.eu1.gigya.com/socialize.getUserInfo"
        headers = {"User-Agent": BROWSER_USER_AGENT}
        data = {
            "enabledProviders": "*",
            "APIKey": API_KEY,
            "sdk": "js_latest",
            "login_token": login_token,
            "authMode": "cookie",
            "pageURL": "https://aylaopenid.delonghigroup.com/",
            "gmid": gmid,
            "ucid": ucid,
            "sdkBuild": SDK_BUILD,
            "format": "json",
        }
        response = await self.session.post(url, headers=headers, data=data)
        response = await response.text()
        response = json.loads(response)

        user_uid = response["UID"]
        user_uid_signature = response["UIDSignature"]
        user_signature_timestamp = response["signatureTimestamp"]

        # Step 5: Consent
        url = "https://aylaopenid.delonghigroup.com/OIDCConsentPage.php"
        headers = {"User-Agent": BROWSER_USER_AGENT}
        params = {
            "lang": self.language,
            "context": context,
            "clientID": CLIENT_ID,
            "scope": "openid+email+profile+UID+comfort+en+alexa",
            "UID": user_uid,
            "UIDSignature": user_uid_signature,
            "signatureTimestamp": user_signature_timestamp,
        }
        response = await self.session.get(url, headers=headers, params=params)
        response = await response.text()

        signature = response.split("const consentObj2Sig = '")[1].split("';")[0]

        # Step 6: Authorization
        url = f"https://fidm.eu1.gigya.com/oidc/op/v1.0/{API_KEY}/authorize/continue"
        headers = {"User-Agent": BROWSER_USER_AGENT}
        params = {
            "context": context,
            "login_token": login_token,
            "consent": json.dumps(
                {
                    "scope": "openid email profile UID comfort en alexa",
                    "clientID": CLIENT_ID,
                    "context": context,
                    "UID": user_uid,
                    "consent": True,
                },
                separators=(",", ":"),
            ),
            "sig": signature,
            "gmidTicket": gmid_ticket,
        }
        response = await self.session.get(
            url, headers=headers, params=params, allow_redirects=False
        )
        location = response.headers.get("Location")
        code = await self.get_query_param(location, "code")

        # Step 7: Get IDP access token
        url = f"https://fidm.eu1.gigya.com/oidc/op/v1.0/{API_KEY}/token"
        headers = {
            "User-Agent": TOKEN_USER_AGENT,
            "Authorization": AUTHORIZATION_HEADER,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": "https://google.it",
        }
        response = await self.session.post(url, headers=headers, data=data)
        response = await response.text()
        response = json.loads(response)

        idp_token = response["access_token"]

        # Step 8: Exchange IDP token for Ayla token
        url = "https://user-field-eu.aylanetworks.com/api/v1/token_sign_in"
        headers = {"User-Agent": TOKEN_USER_AGENT}
        data = {
            "app_id": APP_ID,
            "app_secret": APP_SECRET,
            "token": idp_token,
        }
        response = await self.session.post(url, headers=headers, data=data)
        response = await response.text()
        response = json.loads(response)

        self.access_token = response["access_token"]
        self.refresh_token = response["refresh_token"]
        self.token_expiry = time.time() + int(response["expires_in"])

        return self.access_token

    async def get_request(self, path: str) -> dict:
        """Send a GET request to the specified path.

        Args:
          path (str): The API endpoint path to send the GET request to.

        Returns:
          dict: The JSON response from the API.

        """
        url = f"https://ads-eu.aylanetworks.com/{path}"
        access_token = await self.get_access_token()
        headers = {
            "User-Agent": API_USER_AGENT,
            "Authorization": f"auth_token {access_token}",
        }
        response = await self.session.get(url, headers=headers)
        return await response.json()

    async def post_request(self, path: str, body: dict) -> dict:
        """Send a POST request to the specified path with the given body.

        Args:
          path (str): The API endpoint path to send the request to.
          body (dict): The JSON-serializable body to include in the request.

        Returns:
          dict: The JSON response from the server.

        """

        url = f"https://ads-eu.aylanetworks.com/{path}"
        access_token = await self.get_access_token()
        headers = {
            "User-Agent": API_USER_AGENT,
            "Authorization": f"auth_token {access_token}",
            "Content-Type": "application/json",
        }
        response = await self.session.post(url, headers=headers, json=body)
        return await response.json()

    async def get_first_device(self) -> str:
        """Retrieve the first device's DSN (Device Serial Number).

        If the device DSN is already cached in the instance, it returns the cached value.
        Otherwise, it makes a request to fetch the list of devices,
        caches the DSN of the first device, and returns it.

        Returns:
          str: The DSN of the first device.

        """
        if self.device_dsn:
            return self.device_dsn

        devices = await self.get_request("apiv1/devices.json")
        self.device_dsn = devices[0]["device"]["dsn"]

        return self.device_dsn

    async def get_property(self, name: str):
        """Retrieve the value of a specific property from the device.

        Args:
          name (str): The name of the property to retrieve.

        Returns:
          The value of the specified property, or None if the property is not found.

        """
        device_properties = await self.get_properties()
        device_property = [
            device_property
            for device_property in device_properties
            if device_property.get("name") == name
        ][0]

        return device_property.get("value")

    async def get_str_property(self, name: str) -> str:
        """Retrieve the str value of a specific property from the device.

        Args:
          name (str): The name of the property to retrieve.

        Returns:
          The value of the specified property.

        """
        return str(await self.get_property(name))

    async def get_int_property(self, name: str) -> int:
        """Retrieve the int value of a specific property from the device.

        Args:
          name (str): The name of the property to retrieve.

        Returns:
          The value of the specified property.

        """
        return int(await self.get_property(name))

    async def get_properties(self) -> dict:
        """Retrieve the properties of the device.

        This method fetches the device properties from the API if they have not been
        retrieved in the last 10 seconds. If the properties were retrieved recently,
        it returns the cached properties.

        Returns:
          list: A list of device properties.

        """
        now = time.time()
        if self.device_properties_timestamp is not None and (
            now - self.device_properties_timestamp < 10
        ):
            return self.device_properties

        device_dsn = await self.get_first_device()
        device_properties = await self.get_request(
            f"apiv1/dsns/{device_dsn}/properties.json"
        )

        self.device_properties = [
            device_property.get("property") for device_property in device_properties
        ]
        self.device_properties_timestamp = time.time()

        return self.device_properties

    async def get_query_param(self, url: str, param: str) -> str | None:
        """Extract the value of a specified query parameter from a given URL.

        Args:
          url (str): The URL containing the query parameters.
          param (str): The name of the query parameter to retrieve.

        Returns:
          str or None: The value of the specified query parameter if it exists,
                 otherwise None.

        """
        query = urllib.parse.urlparse(url).query
        params = urllib.parse.parse_qs(query)
        return params.get(param, [None])[0]

    async def url_encode(self, value: str) -> str:
        """URL-encodes a given string.

        Args:
          value (str): The string to be URL-encoded.

        Returns:
          str: The URL-encoded string.

        """
        return urllib.parse.quote(value)
