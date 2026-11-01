"""What the native app may and may not contain, checked from its sources.

Rainette is free, ad-free and needs no account or paid service. Those are
promises a dependency can break silently -- one advertising or analytics SDK
pulled in for convenience, one "fallback" resolver that sends every song to
somebody else's server -- so the allowed dependencies are listed here and
anything else fails the build. Adding one means arguing for it in this file.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOBILE = ROOT / "mobile"
APP = MOBILE / "android" / "app"
SOURCES = APP / "src" / "main"

# group:artifact prefixes the Android app may depend on, and why.
ALLOWED_DEPENDENCIES = {
    "androidx.": "AndroidX and Media3: the platform's own libraries",
    "com.github.TeamNewPipe:NewPipeExtractor:": "on-device search and stream resolution (GPL-3.0)",
    "com.android.tools:desugar_jdk_libs": "java.time/java.nio for older Android, needed by the extractor",
    "junit:junit:": "tests only",
    "org.robolectric:robolectric:": "tests only",
}

FORBIDDEN = re.compile(
    r"admob|play-services-ads|googleads|com\.google\.gms|firebase|crashlytics|"
    r"facebook|applovin|unity3d\.ads|ironsource|mopub|chartboost|vungle|"
    r"appsflyer|adjust\.sdk|amplitude|mixpanel|segment\.analytics|sentry|bugsnag",
    re.IGNORECASE,
)


def gradle_dependencies(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return re.findall(r"^\s*(?:implementation|testImplementation|androidTestImplementation|coreLibraryDesugaring)"
                      r"\s+['\"]([^'\"]+)['\"]", text, re.MULTILINE)


def own_sources():
    for path in [*(SOURCES / "java").rglob("*.kt"), *(SOURCES / "java").rglob("*.java"),
                 SOURCES / "AndroidManifest.xml", APP / "build.gradle", MOBILE / "android" / "build.gradle",
                 MOBILE / "package.json", ROOT / "pwa" / "src" / "native-engine.js",
                 ROOT / "pwa" / "src" / "source-provider.js"]:
        yield path


class TestNoAdsNoPaidServices(unittest.TestCase):
    def test_every_dependency_is_on_the_allowed_list(self):
        for dependency in gradle_dependencies(APP / "build.gradle"):
            with self.subTest(dependency=dependency):
                self.assertTrue(any(dependency.startswith(prefix) for prefix in ALLOWED_DEPENDENCIES),
                                f"{dependency} is not on the allowed list in this test")

    def test_the_web_side_needs_only_capacitor(self):
        package = json.loads((MOBILE / "package.json").read_text(encoding="utf-8"))
        names = {*package.get("dependencies", {}), *package.get("devDependencies", {})}
        self.assertTrue(names, "package.json lists nothing")
        self.assertTrue(all(name.startswith("@capacitor/") for name in names), names)

    def test_no_advertising_or_analytics_sdk_is_referenced(self):
        for path in own_sources():
            with self.subTest(path=path.relative_to(ROOT)):
                match = FORBIDDEN.search(path.read_text(encoding="utf-8"))
                self.assertIsNone(match, f"{path.name} mentions {match.group(0) if match else ''}")

    def test_versions_are_pinned_exactly(self):
        for dependency in gradle_dependencies(APP / "build.gradle"):
            with self.subTest(dependency=dependency):
                self.assertNotRegex(dependency, r"[+]|latest|SNAPSHOT", "a floating version changes under us")
        package = json.loads((MOBILE / "package.json").read_text(encoding="utf-8"))
        for name, version in {**package["dependencies"], **package["devDependencies"]}.items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+$", f"{name} is not pinned")


class TestNoOtherServer(unittest.TestCase):
    def test_the_resolver_talks_to_youtube_and_nothing_else(self):
        """No undeclared extraction server, no third-party fallback, no proxy."""
        for path in (SOURCES / "java").rglob("*.kt"):
            text = path.read_text(encoding="utf-8")
            for url in re.findall(r"https?://[^\s\"'$]+", text):
                with self.subTest(path=path.name, url=url):
                    host = re.sub(r"^https?://", "", url).split("/")[0]
                    self.assertTrue(host.endswith("youtube.com") or host.endswith(".invalid") or host == "",
                                    f"{path.name} reaches {host}")

    def test_the_page_never_hands_the_service_a_url(self):
        """The service derives where audio comes from; a page cannot point it anywhere."""
        engine = (ROOT / "pwa" / "src" / "native-engine.js").read_text(encoding="utf-8")
        wire = engine[engine.index("function wire(track)"):]
        wire = wire[: wire.index("\n}\n")]
        self.assertNotIn("url:", wire.replace("thumbnail_url", ""))
        plugin = (SOURCES / "java" / "com" / "krysisux" / "rainettemusic" / "RainettePlayerPlugin.kt").read_text(encoding="utf-8")
        self.assertIn("isValidId(id)", plugin)
        self.assertNotIn("setUri(", plugin, "only the service sets a URI, from the track id")


class TestDistribution(unittest.TestCase):
    def test_the_android_app_is_gpl_3(self):
        license_text = (MOBILE / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3, 29 June 2007", license_text)
        package = json.loads((MOBILE / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["license"], "GPL-3.0-only")

    def test_no_signing_material_is_committed(self):
        ignored = (MOBILE / "android" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("local.properties", ignored)
        for pattern in ("*.jks", "*.keystore", "*.p12", "*.pem"):
            self.assertEqual(list(MOBILE.glob(f"android/**/{pattern}")), [], pattern)

    def test_version_codes_only_ever_rise_with_the_version(self):
        """Android refuses an update whose versionCode is not higher than the installed one."""
        build = (APP / "build.gradle").read_text(encoding="utf-8")
        line = next(l for l in build.splitlines() if l.strip().startswith("versionCode"))
        self.assertIn("* 10000", line)
        self.assertIn("* 100 +", line)
        self.assertIn("minor and patch must be below 100", build)

    def test_debug_builds_install_beside_a_released_app(self):
        build = (APP / "build.gradle").read_text(encoding="utf-8")
        self.assertRegex(build, r"debug\s*\{[^}]*applicationIdSuffix\s+'\.dev'")

    def test_backups_cannot_carry_pairing_tokens_off_the_phone(self):
        manifest = (SOURCES / "AndroidManifest.xml").read_text(encoding="utf-8")
        self.assertIn('android:allowBackup="false"', manifest)

    def test_the_media_service_refuses_untrusted_controllers(self):
        service = (SOURCES / "java" / "com" / "krysisux" / "rainettemusic" / "PlaybackService.kt").read_text(encoding="utf-8")
        self.assertIn("controller.isTrusted", service)
        self.assertIn("MediaSession.ConnectionResult.reject()", service)


class TestReleaseSigning(unittest.TestCase):
    BUILD = APP / "build.gradle"
    WORKFLOW = ROOT / ".github" / "workflows" / "android.yml"

    def test_the_app_id_is_new_not_the_unrecoverable_one(self):
        """0.2.6's key is gone; reusing its id would promise an update Android refuses."""
        build = self.BUILD.read_text(encoding="utf-8")
        self.assertRegex(build, r'applicationId "com\.krysisux\.rainette"')
        self.assertNotRegex(build, r'applicationId "com\.krysisux\.rainettemusic"')

    def test_the_expected_signer_is_committed_and_well_formed(self):
        fingerprint = (MOBILE / "android" / "release-cert.sha256").read_text(encoding="utf-8").strip()
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_no_key_material_or_password_is_in_the_build(self):
        build = self.BUILD.read_text(encoding="utf-8")
        for line in build.splitlines():
            if re.match(r"\s*(storePassword|keyPassword)\b", line):
                self.assertIn("System.getenv(", line, f"secret not from the environment: {line.strip()}")
        self.assertIn("System.getenv('RAINETTE_ANDROID_KEYSTORE')", build)

    def test_ci_verifies_the_signer_and_never_publishes_a_tag_build_by_itself(self):
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("secrets.ANDROID_RELEASE_KEYSTORE_BASE64", workflow)
        self.assertIn("release-cert.sha256", workflow)
        self.assertIn("application-debuggable", workflow)
        # Uploading to a release happens only in the manual publish job.
        publish = workflow[workflow.index("  publish:"):]
        self.assertEqual(workflow.count("gh release upload"), publish.count("gh release upload"))
        self.assertIn("github.event_name == 'workflow_dispatch'", publish)
        self.assertIn("sha256sum --check", publish)


IOS = MOBILE / "ios"
IOS_CORE = IOS / "RainettePlayer"

# Swift packages the iPhone app may depend on, and why.
ALLOWED_SWIFT_PACKAGES = {
    "https://github.com/alexeichhorn/YouTubeKit.git": "on-device stream resolution (MIT), local extractor only",
    "https://github.com/ionic-team/capacitor-swift-pm.git": "Capacitor itself",
}


def swift_sources():
    yield from (IOS_CORE / "Sources").rglob("*.swift")
    yield from (IOS / "App" / "App").glob("*.swift")


class TestTheIPhoneApp(unittest.TestCase):
    def test_every_swift_package_is_allowed_and_pinned_exactly(self):
        for manifest in (IOS_CORE / "Package.swift", IOS / "App" / "CapApp-SPM" / "Package.swift"):
            text = manifest.read_text(encoding="utf-8")
            for url, rule in re.findall(r'\.package\(url:\s*"([^"]+)",\s*([a-z]+):', text):
                with self.subTest(package=url):
                    self.assertIn(url, ALLOWED_SWIFT_PACKAGES, f"{url} is not on the allowed list in this test")
                    self.assertEqual(rule, "exact", f"{url} must be pinned to one version")

    def test_youtubekits_remote_fallback_is_never_enabled(self):
        # Its remote method sends every song to somebody else's server.
        uses = []
        for path in swift_sources():
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\.remote\b", f"{path.name} reaches for the remote extractor")
            uses += re.findall(r"YouTube\(videoID:[^)]*\)", text)
        self.assertTrue(uses, "the resolver no longer calls YouTubeKit")
        self.assertTrue(all("methods: [.local]" in use for use in uses), uses)

    def test_no_advertising_or_analytics_sdk_is_referenced(self):
        for path in [*swift_sources(), IOS_CORE / "Package.swift"]:
            with self.subTest(path=path.name):
                self.assertIsNone(FORBIDDEN.search(path.read_text(encoding="utf-8")))

    def test_it_keeps_playing_with_the_screen_off(self):
        import plistlib

        info = plistlib.loads((IOS / "App" / "App" / "Info.plist").read_bytes())
        self.assertIn("audio", info.get("UIBackgroundModes", []))

    def test_it_speaks_the_same_protocol_as_the_page(self):
        page = (ROOT / "pwa" / "src" / "native-engine.js").read_text(encoding="utf-8")
        plugin = (IOS / "App" / "App" / "RainettePlayerPlugin.swift").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"const PROTOCOL = (\d+);", page).group(1),
                         re.search(r"static let protocolVersion = (\d+)", plugin).group(1))
        # Every method the page calls is one the plugin offers.
        offered = set(re.findall(r'CAPPluginMethod\(name: "(\w+)"', plugin))
        called = set(re.findall(r"call\('(\w+)'", page)) | {"watch", "setSystemBarColor"}
        self.assertLessEqual(called, offered, called - offered)


    def test_ci_builds_it_and_only_the_manual_job_publishes(self):
        workflow = (ROOT / ".github" / "workflows" / "ios.yml").read_text(encoding="utf-8")
        self.assertIn("CODE_SIGNING_ALLOWED=NO", workflow)
        self.assertIn("UIBackgroundModes", workflow)
        publish = workflow[workflow.index("  publish:"):]
        self.assertEqual(workflow.count("gh release upload"), publish.count("gh release upload"))
        self.assertIn("github.event_name == 'workflow_dispatch'", publish)
        self.assertIn("sha256sum --check", publish)


class TestPairingFromTheApp(unittest.TestCase):
    def test_the_computer_answers_the_apps_exact_origin_and_still_no_wildcard(self):
        import server

        config = json.loads((MOBILE / "capacitor.config.json").read_text(encoding="utf-8"))
        origin = f"{config['server']['androidScheme']}://{config['server']['hostname']}"
        self.assertEqual(origin, server.NATIVE_APP_ORIGIN)
        # Capacitor's iOS scheme, since iOS reserves https for the network.
        self.assertNotIn("iosScheme", config["server"])
        self.assertEqual(f"capacitor://{config['server']['hostname']}", server.NATIVE_IOS_ORIGIN)
        origins = server.configured_pwa_origins()
        self.assertIn(server.NATIVE_APP_ORIGIN, origins)
        self.assertIn(server.NATIVE_IOS_ORIGIN, origins)
        # Its scheme is allowed for that one origin only.
        for lookalike in ("capacitor://evil.example", "capacitor://rainette.localhost.evil.example"):
            with self.assertRaises(ValueError):
                server.normalize_origin(lookalike)
        self.assertIn(server.normalize_origin(server.DEFAULT_PWA_URL), origins)
        self.assertFalse(any("*" in o for o in origins))
        # Plain localhost stays refused: any local web server could claim that one.
        self.assertNotIn("https://localhost", origins)


if __name__ == "__main__":
    unittest.main()
