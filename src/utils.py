import base64
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import urllib.parse

import requests
from selenium.webdriver.chrome.webdriver import WebDriver
import undetected_chromedriver as uc

FLARESOLVERR_VERSION = None
PLATFORM_VERSION = None
CHROME_EXE_PATH = None
CHROME_MAJOR_VERSION = None
USER_AGENT = None
XVFB_DISPLAY = None
PATCHED_DRIVER_PATH = None


def get_config_log_html() -> bool:
    return os.environ.get('LOG_HTML', 'false').lower() == 'true'


def get_config_headless() -> bool:
    return os.environ.get('HEADLESS', 'true').lower() == 'true'


def get_config_disable_media() -> bool:
    return os.environ.get('DISABLE_MEDIA', 'false').lower() == 'true'


def get_flaresolverr_version() -> str:
    global FLARESOLVERR_VERSION
    if FLARESOLVERR_VERSION is not None:
        return FLARESOLVERR_VERSION

    package_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'package.json')
    if not os.path.isfile(package_path):
        package_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'package.json')
    with open(package_path) as f:
        FLARESOLVERR_VERSION = json.loads(f.read())['version']
        return FLARESOLVERR_VERSION

def get_current_platform() -> str:
    global PLATFORM_VERSION
    if PLATFORM_VERSION is not None:
        return PLATFORM_VERSION
    PLATFORM_VERSION = os.name
    return PLATFORM_VERSION


def create_proxy_extension(proxy: dict) -> str:
    parsed_url = urllib.parse.urlparse(proxy['url'])
    scheme = parsed_url.scheme
    host = parsed_url.hostname
    port = parsed_url.port
    username = proxy['username']
    password = proxy['password']
    manifest_json = """
    {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Chrome Proxy",
        "permissions": [
            "proxy",
            "tabs",
            "storage",
            "webRequest",
            "webRequestAuthProvider"
        ],
        "host_permissions": [
          "<all_urls>"
        ],
        "background": {
          "service_worker": "background.js"
        },
        "minimum_chrome_version": "76.0.0"
    }
    """

    background_js = """
    var config = {
        mode: "fixed_servers",
        rules: {
            singleProxy: {
                scheme: "%s",
                host: "%s",
                port: %d
            },
            bypassList: ["localhost"]
        }
    };

    chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});

    function callbackFn(details) {
        return {
            authCredentials: {
                username: "%s",
                password: "%s"
            }
        };
    }

    chrome.webRequest.onAuthRequired.addListener(
        callbackFn,
        { urls: ["<all_urls>"] },
        ['blocking']
    );
    """ % (
        scheme,
        host,
        port,
        username,
        password
    )

    proxy_extension_dir = tempfile.mkdtemp()

    with open(os.path.join(proxy_extension_dir, "manifest.json"), "w") as f:
        f.write(manifest_json)

    with open(os.path.join(proxy_extension_dir, "background.js"), "w") as f:
        f.write(background_js)

    return proxy_extension_dir


def get_webdriver(proxy: dict = None) -> WebDriver:
    global PATCHED_DRIVER_PATH, USER_AGENT
    logging.debug('Launching web browser...')

    # undetected_chromedriver
    options = uc.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-search-engine-choice-screen')
    # todo: this param shows a warning in chrome head-full
    options.add_argument('--disable-setuid-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    # this option removes the zygote sandbox (it seems that the resolution is a bit faster)
    options.add_argument('--no-zygote')
    # attempt to fix Docker ARM32 build
    IS_ARMARCH = platform.machine().startswith(('arm', 'aarch'))
    if IS_ARMARCH:
        options.add_argument('--disable-gpu-sandbox')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument('--ignore-ssl-errors')

    language = os.environ.get('LANG', None)
    if language is not None:
        options.add_argument('--accept-lang=%s' % language)

    # Fix for Chrome 117 | https://github.com/FlareSolverr/FlareSolverr/issues/910
    if USER_AGENT is not None:
        options.add_argument('--user-agent=%s' % USER_AGENT)

    proxy_extension_dir = None
    if proxy and all(key in proxy for key in ['url', 'username', 'password']):
        proxy_extension_dir = create_proxy_extension(proxy)
        options.add_argument("--disable-features=DisableLoadExtensionCommandLineSwitch")
        options.add_argument("--load-extension=%s" % os.path.abspath(proxy_extension_dir))
    elif proxy and 'url' in proxy:
        proxy_url = proxy['url']
        logging.debug("Using webdriver proxy: %s", proxy_url)
        options.add_argument('--proxy-server=%s' % proxy_url)

    # note: headless mode is detected (headless = True)
    # we launch the browser in head-full mode with the window hidden
    windows_headless = False
    if get_config_headless():
        if os.name == 'nt':
            windows_headless = True
        else:
            start_xvfb_display()
    # For normal headless mode:
    # options.add_argument('--headless')

    # if we are inside the Docker container, we avoid downloading the driver
    driver_exe_path = None
    version_main = None
    if os.path.exists("/app/chromedriver"):
        # running inside Docker
        driver_exe_path = "/app/chromedriver"
    else:
        version_main = get_chrome_major_version()
        if PATCHED_DRIVER_PATH is not None:
            driver_exe_path = PATCHED_DRIVER_PATH

    # detect chrome path
    browser_executable_path = get_chrome_exe_path()

    # downloads and patches the chromedriver
    # if we don't set driver_executable_path it downloads, patches, and deletes the driver each time
    try:
        driver = uc.Chrome(options=options, browser_executable_path=browser_executable_path,
                           driver_executable_path=driver_exe_path, version_main=version_main,
                           windows_headless=windows_headless, headless=get_config_headless())
    except Exception as e:
        logging.error("Error starting Chrome: %s" % e)
        # No point in continuing if we cannot retrieve the driver
        raise e

    # save the patched driver to avoid re-downloads
    if driver_exe_path is None:
        PATCHED_DRIVER_PATH = os.path.join(driver.patcher.data_path, driver.patcher.exe_name)
        if PATCHED_DRIVER_PATH != driver.patcher.executable_path:
            shutil.copy(driver.patcher.executable_path, PATCHED_DRIVER_PATH)

    # clean up proxy extension directory
    if proxy_extension_dir is not None:
        shutil.rmtree(proxy_extension_dir)

    # selenium vanilla
    # options = webdriver.ChromeOptions()
    # options.add_argument('--no-sandbox')
    # options.add_argument('--window-size=1920,1080')
    # options.add_argument('--disable-setuid-sandbox')
    # options.add_argument('--disable-dev-shm-usage')
    # driver = webdriver.Chrome(options=options)

    return driver


def get_chrome_exe_path() -> str:
    global CHROME_EXE_PATH
    if CHROME_EXE_PATH is not None:
        return CHROME_EXE_PATH
    # linux pyinstaller bundle
    chrome_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chrome', "chrome")
    if os.path.exists(chrome_path):
        if not os.access(chrome_path, os.X_OK):
            raise Exception(f'Chrome binary "{chrome_path}" is not executable. '
                            f'Please, extract the archive with "tar xzf <file.tar.gz>".')
        CHROME_EXE_PATH = chrome_path
        return CHROME_EXE_PATH
    # windows pyinstaller bundle
    chrome_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chrome', "chrome.exe")
    if os.path.exists(chrome_path):
        CHROME_EXE_PATH = chrome_path
        return CHROME_EXE_PATH
    # system
    CHROME_EXE_PATH = uc.find_chrome_executable()
    return CHROME_EXE_PATH


def get_chrome_major_version() -> str:
    global CHROME_MAJOR_VERSION
    if CHROME_MAJOR_VERSION is not None:
        return CHROME_MAJOR_VERSION

    if os.name == 'nt':
        # Example: '104.0.5112.79'
        try:
            complete_version = extract_version_nt_executable(get_chrome_exe_path())
        except Exception:
            try:
                complete_version = extract_version_nt_registry()
            except Exception:
                # Example: '104.0.5112.79'
                complete_version = extract_version_nt_folder()
    else:
        chrome_path = get_chrome_exe_path()
        process = os.popen(f'"{chrome_path}" --version')
        # Example 1: 'Chromium 104.0.5112.79 Arch Linux\n'
        # Example 2: 'Google Chrome 104.0.5112.79 Arch Linux\n'
        complete_version = process.read()
        process.close()

    CHROME_MAJOR_VERSION = complete_version.split('.')[0].split(' ')[-1]
    return CHROME_MAJOR_VERSION


def extract_version_nt_executable(exe_path: str) -> str:
    import pefile
    pe = pefile.PE(exe_path, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
    )
    return pe.FileInfo[0][0].StringTable[0].entries[b"FileVersion"].decode('utf-8')


def extract_version_nt_registry() -> str:
    stream = os.popen(
        'reg query "HKLM\\SOFTWARE\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Google Chrome"')
    output = stream.read()
    google_version = ''
    for letter in output[output.rindex('DisplayVersion    REG_SZ') + 24:]:
        if letter != '\n':
            google_version += letter
        else:
            break
    return google_version.strip()


def extract_version_nt_folder() -> str:
    # Check if the Chrome folder exists in the x32 or x64 Program Files folders.
    for i in range(2):
        path = 'C:\\Program Files' + (' (x86)' if i else '') + '\\Google\\Chrome\\Application'
        if os.path.isdir(path):
            paths = [f.path for f in os.scandir(path) if f.is_dir()]
            for path in paths:
                filename = os.path.basename(path)
                pattern = r'\d+\.\d+\.\d+\.\d+'
                match = re.search(pattern, filename)
                if match and match.group():
                    # Found a Chrome version.
                    return match.group(0)
    return ''


def _default_user_agent() -> str:
    major = get_chrome_major_version() or '120'
    if PLATFORM_VERSION == 'nt' or (PLATFORM_VERSION is None and os.name == 'nt'):
        platform_token = 'Windows NT 10.0; Win64; x64'
    else:
        platform_token = 'X11; Linux x86_64'
    return (
        f'Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 (KHTML, like Gecko) '
        f'Chrome/{major}.0.0.0 Safari/537.36'
    )


def _safe_quit_driver(driver: WebDriver) -> None:
    try:
        if PLATFORM_VERSION == 'nt':
            try:
                driver.close()
            except Exception:
                pass
        driver.quit()
    except Exception:
        pass


def get_user_agent(driver=None) -> str:
    global USER_AGENT
    if USER_AGENT is not None:
        return USER_AGENT

    owned_driver = driver is None
    try:
        if driver is None:
            driver = get_webdriver()
        try:
            driver.get('about:blank')
        except Exception:
            pass
        ua = driver.execute_script('return navigator.userAgent')
        if not ua:
            USER_AGENT = _default_user_agent()
            logging.warning('navigator.userAgent was empty; using fallback User-Agent')
        else:
            # Fix for Chrome 117 | https://github.com/FlareSolverr/FlareSolverr/issues/910
            USER_AGENT = re.sub('HEADLESS', '', ua, flags=re.IGNORECASE)
        return USER_AGENT
    except Exception as e:
        USER_AGENT = _default_user_agent()
        logging.warning('Error getting browser User-Agent (%s); using fallback', e)
        return USER_AGENT
    finally:
        if owned_driver and driver is not None:
            _safe_quit_driver(driver)


def start_xvfb_display():
    global XVFB_DISPLAY
    if XVFB_DISPLAY is None:
        from xvfbwrapper import Xvfb
        XVFB_DISPLAY = Xvfb()
        XVFB_DISPLAY.start()


IMAGE_URL_EXTENSIONS = (
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico',
    '.avif', '.jfif', '.tiff', '.tif',
)


def is_image_url(url: str) -> bool:
    if not url:
        return False
    path = urllib.parse.urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in IMAGE_URL_EXTENSIONS)


def is_image_content_type(content_type: str) -> bool:
    if not content_type:
        return False
    return content_type.lower().split(';')[0].strip().startswith('image/')


def _guess_image_mime(url: str, content_type: str | None = None) -> str:
    if content_type and is_image_content_type(content_type):
        return content_type.split(';')[0].strip()
    ext = urllib.parse.urlparse(url).path.lower()
    mime_by_ext = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.jfif': 'image/jpeg',
        '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
        '.bmp': 'image/bmp', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
        '.avif': 'image/avif', '.tiff': 'image/tiff', '.tif': 'image/tiff',
    }
    for suffix, guessed in mime_by_ext.items():
        if ext.endswith(suffix):
            return guessed
    return 'application/octet-stream'


def get_image_download_urls(driver: WebDriver, req_url: str | None) -> list[str]:
    """Collect direct image URLs to download (request URL, current URL, <img src>)."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        if not url or not url.startswith('http') or url in seen:
            return
        seen.add(url)
        candidates.append(url)

    if req_url:
        add(req_url)
    try:
        add(driver.current_url)
    except Exception:
        pass
    try:
        from selenium.webdriver.common.by import By
        for img in driver.find_elements(By.CSS_SELECTOR, 'img[src]'):
            add(img.get_attribute('src'))
    except Exception:
        pass
    return candidates


def fetch_image_as_base64(driver: WebDriver, url: str) -> tuple[str | None, str | None]:
    """
    Download image bytes using the browser session cookies.
    Returns (base64_string, mime_type) or (None, None) if not an image or on failure.
    """
    if not is_image_url(url):
        return None, None

    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie['name'],
            cookie['value'],
            domain=cookie.get('domain'),
        )
    referer = driver.current_url if driver else url
    headers = {
        'User-Agent': get_user_agent(driver),
        'Referer': referer,
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    }
    try:
        resp = session.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        if not is_image_content_type(content_type) and not is_image_url(url):
            return None, None
        mime = _guess_image_mime(url, content_type)
        payload = base64.b64encode(resp.content).decode('ascii')
        return payload, mime
    except Exception as e:
        logging.warning('Failed to fetch image as base64 from %s: %s', url, e)
        return None, None


def fetch_image_as_base64_via_browser(driver: WebDriver, url: str) -> tuple[str | None, str | None]:
    """Fetch image inside the browser context (same cookies / CORS as the page)."""
    if not is_image_url(url):
        return None, None
    try:
        result = driver.execute_async_script("""
            var imageUrl = arguments[0];
            var done = arguments[arguments.length - 1];
            fetch(imageUrl, { credentials: 'include', mode: 'cors' })
                .then(function(response) {
                    if (!response.ok) {
                        done({ ok: false, error: 'HTTP ' + response.status });
                        return;
                    }
                    return response.blob().then(function(blob) {
                        var reader = new FileReader();
                        reader.onloadend = function() {
                            var dataUrl = reader.result || '';
                            var comma = dataUrl.indexOf(',');
                            done({
                                ok: true,
                                b64: comma >= 0 ? dataUrl.substring(comma + 1) : dataUrl,
                                type: blob.type || ''
                            });
                        };
                        reader.onerror = function() { done({ ok: false, error: 'read failed' }); };
                        reader.readAsDataURL(blob);
                    });
                })
                .catch(function(err) { done({ ok: false, error: String(err) }); });
        """, url)
        if not result or not result.get('ok') or not result.get('b64'):
            logging.warning('Browser fetch for image failed: %s', result)
            return None, None
        mime = _guess_image_mime(url, result.get('type'))
        return result['b64'], mime
    except Exception as e:
        logging.warning('Browser fetch image error for %s: %s', url, e)
        return None, None


def try_set_solution_image_response(driver: WebDriver, challenge_res, req_url: str | None) -> bool:
    """Set challenge_res.response to base64 when the request URL is an image."""
    if not req_url or not is_image_url(req_url):
        return False
    for url in get_image_download_urls(driver, req_url):
        if not is_image_url(url):
            continue
        for fetcher in (fetch_image_as_base64, fetch_image_as_base64_via_browser):
            image_b64, image_mime = fetcher(driver, url)
            if image_b64:
                challenge_res.response = image_b64
                logging.info('Image URL stored as base64 in response from %s (%s)', url, image_mime)
                return True
    return False


def object_to_dict(_object):
    json_dict = json.loads(json.dumps(_object, default=lambda o: o.__dict__))
    # remove hidden fields
    return {k: v for k, v in json_dict.items() if not k.startswith('__')}
