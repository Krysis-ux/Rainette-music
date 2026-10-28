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

    def test_a_build_can_never_downgrade_the_old_app(self):
        """0.2.6 shipped as versionCode 3; Android refuses an update with a lower one."""
        build = (APP / "build.gradle").read_text(encoding="utf-8")
        code = int(re.search(r"^\s*versionCode\s+(\d+)", build, re.MULTILINE).group(1))
        self.assertGreater(code, 3)

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


class TestPairingFromTheApp(unittest.TestCase):
    def test_the_computer_answers_the_apps_exact_origin_and_still_no_wildcard(self):
        import server

        config = json.loads((MOBILE / "capacitor.config.json").read_text(encoding="utf-8"))
        origin = f"{config['server']['androidScheme']}://{config['server']['hostname']}"
        self.assertEqual(origin, server.NATIVE_APP_ORIGIN)
        origins = server.configured_pwa_origins()
        self.assertIn(server.NATIVE_APP_ORIGIN, origins)
        self.assertIn(server.normalize_origin(server.DEFAULT_PWA_URL), origins)
        self.assertFalse(any("*" in o for o in origins))
        # Plain localhost stays refused: any local web server could claim that one.
        self.assertNotIn("https://localhost", origins)


if __name__ == "__main__":
    unittest.main()
