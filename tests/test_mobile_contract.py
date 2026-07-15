import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = ROOT / "mobile"


def _workflow_step(workflow, name):
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - name: ", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def _braced_block(source, marker):
    start = source.index(marker)
    opening = source.index("{", start + len(marker))
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError(f"Unclosed block after {marker!r}")


class MobileContractTests(unittest.TestCase):
    def test_android_companion_deep_link_and_secure_native_client_contract(self):
        manifest = (MOBILE / "android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
        plugin = (MOBILE / "android" / "app" / "src" / "main" / "java" / "com" / "krysisux" / "rainettemusic" / "RainetteCompanionPlugin.java").read_text(encoding="utf-8")
        self.assertIn('android.intent.action.VIEW', manifest)
        self.assertIn('android.intent.category.BROWSABLE', manifest)
        self.assertIn('android:scheme="rainette"', manifest)
        self.assertIn('android:host="pair"', manifest)
        for marker in (
            "AndroidKeyStore", "RSA/ECB/OAEPWithSHA-256AndMGF1Padding",
            "certificate_sha256", "X509TrustManager", "/pair/request", "/pair/result",
            "/pair/ack", "EncryptedSharedPreferences", "Authorization", "Bearer ",
        ):
            self.assertIn(marker, plugin)
        persisted = plugin.index('.putString("device_token", token)')
        acknowledged = plugin.index('new URL(endpoint + "/pair/ack")')
        self.assertLess(persisted, acknowledged)
        self.assertIn("MGF1ParameterSpec.SHA1", plugin)
        self.assertNotIn("MGF1ParameterSpec.SHA256", plugin)
        self.assertNotIn("ALLOW_ALL_HOSTNAME_VERIFIER", plugin)
        self.assertNotIn("return true; // disable", plugin)

    def test_android_pairing_status_progress_and_revision_recovery_contract(self):
        plugin = (MOBILE / "android" / "app" / "src" / "main" / "java" / "com" / "krysisux" / "rainettemusic" / "RainetteCompanionPlugin.java").read_text(encoding="utf-8")
        status = _braced_block(plugin, "public void connectionStatus(PluginCall call)")
        self.assertIn('new URL(endpoint + "/status")', status)
        self.assertIn('setRequestProperty("Authorization", "Bearer " + token)', status)
        self.assertIn('deviceId.equals(authenticatedDeviceId)', status)
        self.assertIn('state.put("paired", true)', status)
        self.assertIn('"reconnecting"', status)
        for phase in ("connecting", "pending_approval", "securing"):
            self.assertIn(f'emitPairingProgress("{phase}"', plugin)
        self.assertIn('"rainette_companion_pairing"', plugin)

        sync = _braced_block(plugin, "private void runSyncLoop()")
        self.assertIn("activeEndpoint", sync)
        self.assertIn("activeDeviceId", sync)
        self.assertIn("activeToken", sync)
        self.assertIn("revision = 0L", sync)
        self.assertIn('latest.getString("endpoint", "")', sync)
        self.assertIn('latest.getString("device_id", "")', sync)
        self.assertIn('latest.getString("device_token", "")', sync)
        self.assertIn('response.optBoolean("reset_required", false)', sync)
        self.assertRegex(
            sync,
            re.compile(r"revision\s*=\s*response\.optBoolean.*?\? responseRevision\s*:\s*Math\.max", re.DOTALL),
        )

    def test_android_pair_ack_is_durable_retried_and_reconciled_after_restart(self):
        plugin = (MOBILE / "android" / "app" / "src" / "main" / "java" / "com" / "krysisux" / "rainettemusic" / "RainetteCompanionPlugin.java").read_text(encoding="utf-8")
        load = _braced_block(plugin, "public void load()")
        self.assertIn("reconcilePendingAcknowledgement", load)
        self.assertIn('putString("pending_ack_request_id", requestId)', plugin)
        self.assertIn('getString("pending_ack_request_id", "")', plugin)
        self.assertIn('remove("pending_ack_request_id")', plugin)
        self.assertIn("acknowledgePairingWithRetry", plugin)
        retry = _braced_block(plugin, "private boolean acknowledgePairingWithRetry(")
        self.assertIn("ACK_MAX_ATTEMPTS", retry)
        self.assertIn("ACK_BACKOFF_MS", retry)
        self.assertIn("Thread.sleep", retry)
        self.assertIn("isTransientAckStatus", retry)
        self.assertIn("Authorization", retry)
        self.assertIn("Bearer ", retry)
        terminal = retry.index("if (!isTransientAckStatus(status)) return true;")
        response_body = retry.rindex("readJson(acknowledgement, status)")
        self.assertLess(terminal, response_body)
        self.assertNotIn('remove("device_token")', plugin)

    def test_native_command_transport_posts_the_raw_music_payload(self):
        plugin = (MOBILE / "android" / "app" / "src" / "main" / "java" / "com" / "krysisux" / "rainettemusic" / "RainetteCompanionPlugin.java").read_text(encoding="utf-8")
        source = (ROOT / "web" / "rainette_platform.js").read_text(encoding="utf-8")
        request_block = _braced_block(plugin, "public void request(PluginCall call)")
        self.assertIn('new URL(endpoint + "/command")', request_block)
        self.assertIn("validateCompanionEndpoint(endpoint)", request_block)
        self.assertIn("writeJson(connection", request_block)
        self.assertIn("setReadTimeout(30_000)", request_block)
        self.assertNotIn('getString("path"', request_block)
        self.assertIn("companion.request({ payload })", source)
        self.assertNotIn("body: payload", source)

    def test_pairing_rejects_public_hosts_and_confirms_before_replacing_credentials(self):
        plugin = (MOBILE / "android" / "app" / "src" / "main" / "java" / "com" / "krysisux" / "rainettemusic" / "RainetteCompanionPlugin.java").read_text(encoding="utf-8")
        for marker in (
            "isAllowedEndpointHost", "Inet6Address", 'endsWith(".local")',
            "AlertDialog.Builder", "confirmAndStartPairing", "Pair with Rainette desktop",
        ):
            self.assertIn(marker, plugin)
        self.assertIn("Pairing endpoint must use a private or local host", plugin)
        self.assertGreaterEqual(plugin.count("confirmAndStartPairing("), 4)
        self.assertIn("finishPairing(call, false, \"cancelled\"", plugin)

    def test_capacitor_workspace_targets_shared_web_and_android(self):
        package = json.loads((MOBILE / "package.json").read_text(encoding="utf-8"))
        config = (MOBILE / "capacitor.config.ts").read_text(encoding="utf-8")
        self.assertIn("@capacitor/core", package["dependencies"])
        self.assertIn("@capacitor/android", package["devDependencies"])
        self.assertIn("webDir: '../web'", config)
        self.assertIn("com.krysisux.rainettemusic", config)

    def test_shared_web_has_a_native_transport_boundary(self):
        source = (ROOT / "web" / "rainette_platform.js").read_text(encoding="utf-8")
        shell = (ROOT / "web" / "music_shell.js").read_text(encoding="utf-8")
        index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("RainetteNativeTransport", source)
        self.assertIn("RainettePlayer", source)
        self.assertIn("RainetteCompanion", source)
        self.assertIn("RainetteNativeTransport", shell)
        self.assertIn("viewport-fit=cover", index)
        self.assertIn("safe-area-inset-bottom", css)

    def test_native_fire_and_forget_send_does_not_enter_websocket_queue_or_publish_empty_results(self):
        shell = (ROOT / "web" / "music_shell.js").read_text(encoding="utf-8")
        native_branch = shell[shell.index("if (native) {"):shell.index("\n\tconst ws =", shell.index("if (native) {"))]
        self.assertIn("Promise.resolve(native.request(payload))", native_branch)
        self.assertIn("if (response)", native_branch)
        self.assertNotIn("helperQueue.push", native_branch)

    def test_android_declares_media_session_playback_service(self):
        manifest = (MOBILE / "android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
        service = (MOBILE / "android" / "app" / "src" / "main" / "java" / "com" / "krysisux" / "rainettemusic" / "RainettePlaybackService.java").read_text(encoding="utf-8")
        self.assertIn("FOREGROUND_SERVICE_MEDIA_PLAYBACK", manifest)
        self.assertIn("RainettePlaybackService", manifest)
        self.assertIn("MediaSessionService", service)

    def test_android_release_workflow_signs_and_publishes_expected_apk(self):
        workflow = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")
        for secret in (
            "ANDROID_KEYSTORE_BASE64",
            "ANDROID_KEYSTORE_PASSWORD",
            "ANDROID_KEY_ALIAS",
            "ANDROID_KEY_PASSWORD",
        ):
            self.assertIn(secret, workflow)
        self.assertIn("distribution: 'temurin'", workflow)
        self.assertIn("java-version: '21'", workflow)
        self.assertIn("rainette-music-android.apk", workflow)
        self.assertIn("RainetteMusicSetup.exe", workflow)
        self.assertIn("rainette-music-android.apk.sha256", workflow)
        self.assertIn("RainetteMusicSetup.exe.sha256", workflow)
        self.assertIn("android-release.json", workflow)
        self.assertIn("windows-release.json", workflow)
        self.assertIn("fail_on_unmatched_files: true", workflow)
        self.assertIn("assembleRelease", workflow)

    def test_android_release_workflow_has_version_tag_trigger_and_least_privilege_permissions(self):
        workflow = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^on:\n  push:\n    tags:\n      - 'v\*'$")
        self.assertRegex(workflow, r"(?m)^permissions:\n  contents: read$")
        self.assertEqual(workflow.count("contents: write"), 1)
        publish_job = workflow[workflow.index("  publish:"):]
        self.assertRegex(publish_job, r"(?m)^    permissions:\n      contents: write$")
        self.assertIn("Release tags must use the exact vMAJOR.MINOR.PATCH format.", workflow)
        self.assertIn("version.APP_VERSION", workflow)
        self.assertIn("RAINETTE_VERSION_NAME: ${{ needs.test.outputs.release_version }}", workflow)
        self.assertIn("RAINETTE_VERSION_CODE: ${{ github.run_number }}", workflow)
        self.assertIn("chmod +x ./gradlew", workflow)

    def test_android_signing_secrets_are_scoped_only_to_required_steps(self):
        workflow = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")
        job_header = workflow[workflow.index("jobs:"):workflow.index("    steps:")]
        self.assertNotIn("    env:", job_header)

        expected_secret_names = {
            "Verify Android signing secrets": {
                "ANDROID_KEYSTORE_BASE64",
                "ANDROID_KEYSTORE_PASSWORD",
                "ANDROID_KEY_ALIAS",
                "ANDROID_KEY_PASSWORD",
                "ANDROID_SIGNING_CERT_SHA256",
            },
            "Decode release keystore": {"ANDROID_KEYSTORE_BASE64"},
            "Build signed release APK": {
                "ANDROID_KEYSTORE_PASSWORD",
                "ANDROID_KEY_ALIAS",
                "ANDROID_KEY_PASSWORD",
            },
            "Verify production Android signature and prepare assets": {
                "ANDROID_SIGNING_CERT_SHA256",
            },
        }
        for step_name, expected in expected_secret_names.items():
            step = _workflow_step(workflow, step_name)
            actual = set(re.findall(r"secrets\.(ANDROID_[A-Z0-9_]+)", step))
            self.assertEqual(expected, actual, step_name)

        for step_name in (
            "Check out tagged source",
            "Set up Java",
            "Set up Node.js",
            "Install mobile dependencies",
            "Sync Capacitor Android project",
            "Add Android signing tools to PATH",
            "Upload verified Android release assets",
            "Publish GitHub Release assets",
        ):
            self.assertNotIn("secrets.", _workflow_step(workflow, step_name), step_name)

    def test_android_release_workflow_fails_before_build_when_secrets_are_missing(self):
        workflow = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")
        preflight = workflow.index("Verify Android signing secrets")
        build = workflow.index("assembleRelease")
        self.assertLess(preflight, build)
        for variable in (
            "ANDROID_KEYSTORE_BASE64",
            "ANDROID_KEYSTORE_PASSWORD",
            "ANDROID_KEY_ALIAS",
            "ANDROID_KEY_PASSWORD",
            "ANDROID_SIGNING_CERT_SHA256",
        ):
            self.assertIn(f'test -n "${{{variable}}}"', workflow[preflight:build])

    def test_windows_signing_credentials_are_isolated_from_the_python_build_runner(self):
        workflow = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")
        build_job = workflow[workflow.index("  windows-build:"):workflow.index("  windows:")]
        signing_job = workflow[workflow.index("  windows:"):workflow.index("  publish:")]
        unsigned_step = _workflow_step(workflow, "Build unsigned Windows application with pinned update signer")
        credential_step = _workflow_step(workflow, "Sign application and package Windows release in isolated phase")

        self.assertIn("actions/setup-python@", build_job)
        self.assertIn("python -m pip", build_job)
        self.assertIn("-Phase BuildUnsigned", unsigned_step)
        self.assertIn("vars.WINDOWS_CODESIGN_CERT_SHA256", unsigned_step)
        self.assertNotIn("secrets.", build_job)

        self.assertIn("needs: [test, windows-build]", signing_job)
        self.assertIn("environment: windows-signing", signing_job)
        self.assertIn("Download unsigned Windows application", signing_job)
        self.assertNotIn("actions/setup-python@", signing_job)
        self.assertNotIn("python -m pip", signing_job)
        self.assertNotIn("PyInstaller", signing_job)
        self.assertIn("secrets.WINDOWS_CODESIGN_CERT_BASE64", credential_step)
        self.assertIn("secrets.WINDOWS_CODESIGN_CERT_PASSWORD", credential_step)
        self.assertIn("-Phase SignAndPackage", credential_step)
        self.assertNotIn("secrets.WINDOWS_CODESIGN_CERT_SHA256", workflow)

        unsigned_build = workflow.index("-Phase BuildUnsigned")
        credential_decode = workflow.index("[IO.File]::WriteAllBytes($certificatePath")
        self.assertLess(unsigned_build, credential_decode)

    def test_android_release_workflow_only_uploads_a_verified_signed_apk(self):
        workflow = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")
        verify = workflow.index("apksigner verify")
        rename = workflow.index("rainette-music-android.apk")
        upload = workflow.index("softprops/action-gh-release")
        self.assertLess(verify, rename)
        self.assertLess(rename, upload)
        upload_config = workflow[upload:]
        self.assertIn("          files: |", upload_config)
        self.assertIn("            release-assets/rainette-music-android.apk", upload_config)
        self.assertIn("            release-assets/RainetteMusicSetup.exe", upload_config)
        self.assertNotRegex(upload_config, r"(?m)^          files: .*[*?\[]")
        self.assertNotIn("app-release-unsigned.apk", upload_config)

    def test_android_release_build_uses_environment_backed_signing_config(self):
        gradle = (MOBILE / "android" / "app" / "build.gradle").read_text(encoding="utf-8")
        for variable in (
            "ANDROID_KEYSTORE_PATH",
            "ANDROID_KEYSTORE_PASSWORD",
            "ANDROID_KEY_ALIAS",
            "ANDROID_KEY_PASSWORD",
        ):
            self.assertIn(f"System.getenv('{variable}')", gradle)
        build_types = _braced_block(gradle, "buildTypes")
        release = _braced_block(build_types, "release")
        self.assertIn("signingConfig signingConfigs.release", release)
        self.assertEqual(gradle.count("signingConfig signingConfigs.release"), 1)
        if "debug" in build_types:
            self.assertNotIn("signingConfig", _braced_block(build_types, "debug"))

    def test_android_release_build_fails_when_any_signing_value_is_missing(self):
        gradle = (MOBILE / "android" / "app" / "build.gradle").read_text(encoding="utf-8")
        failure_guard = re.search(
            r"if \(releaseTaskRequested && \[(.*?)\]\.any \{ !it \}\) \{(.*?)\}",
            gradle,
            re.DOTALL,
        )
        self.assertIsNotNone(failure_guard)
        values, failure = failure_guard.groups()
        for variable in (
            "androidKeystorePath",
            "androidKeystorePassword",
            "androidKeyAlias",
            "androidKeyPassword",
        ):
            self.assertIn(variable, values)
        self.assertIn("throw new GradleException", failure)


if __name__ == "__main__":
    unittest.main()
