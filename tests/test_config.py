"""Tests for config merge/write and autoconfig path detection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from bambu_ams_check.autoconfig import (
    detect_device_id,
    detect_user_id_from_studio,
    detect_watch_directories,
    run_autoconfig,
)
from bambu_ams_check.config import (
    DEFAULTS,
    _deep_merge,
    has_access_token,
    load_config,
    mask_token,
    merge_into_local_config,
    save_cloud_tokens,
)


class TestConfigMerge(unittest.TestCase):
    def test_deep_merge_nested(self):
        base = {"cloud": {"access_token": "", "user_id": "1"}, "region": "cn"}
        over = {"cloud": {"access_token": "abc"}}
        merged = _deep_merge(base, over)
        self.assertEqual(merged["cloud"]["access_token"], "abc")
        self.assertEqual(merged["cloud"]["user_id"], "1")
        self.assertEqual(merged["region"], "cn")

    def test_mask_token_never_full(self):
        token = "a" * 40
        masked = mask_token(token)
        self.assertNotEqual(masked, token)
        self.assertNotIn(token, masked)
        self.assertIn("…", masked)

    def test_save_and_merge_local_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            # Seed like example
            path.write_text(
                yaml.safe_dump(
                    {
                        "region": "us",
                        "cloud": {"access_token": "", "user_id": "", "password": "secret"},
                        "watch": {"directories": ["D:/old"]},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            out = save_cloud_tokens(
                access_token="tokentokentoken123456",
                user_id="999",
                refresh_token="refrefrefrefrefref12",
                region="cn",
                account="u@example.com",
                path=path,
            )
            self.assertEqual(out, path)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data["region"], "cn")
            self.assertEqual(data["cloud"]["access_token"], "tokentokentoken123456")
            self.assertEqual(data["cloud"]["user_id"], "999")
            self.assertEqual(data["cloud"]["refresh_token"], "refrefrefrefrefref12")
            self.assertEqual(data["cloud"]["account"], "u@example.com")
            self.assertNotIn("password", data["cloud"])
            # directories preserved
            self.assertEqual(data["watch"]["directories"], ["D:/old"])
            cfg = load_config(path)
            self.assertTrue(has_access_token(cfg))

    def test_merge_does_not_invent_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            path.write_text("region: cn\ncloud:\n  user_id: '1'\n", encoding="utf-8")
            merge_into_local_config(
                {"region": "cn", "watch": {"directories": []}},
                path=path,
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data["cloud"].get("user_id"), "1")
            self.assertFalse(str(data.get("cloud", {}).get("access_token") or "").strip())


class TestAutoconfig(unittest.TestCase):
    def test_detect_user_id_from_digits_folder(self):
        environ = {"APPDATA": r"C:\Users\demo\AppData\Roaming"}
        user_root = Path(r"C:\Users\demo\AppData\Roaming\BambuStudio\user")

        def exists(p: Path) -> bool:
            return str(p).replace("/", "\\") == str(user_root).replace("/", "\\")

        def listdir(p: Path) -> list[str]:
            self.assertEqual(str(p).replace("/", "\\"), str(user_root).replace("/", "\\"))
            return ["cache", "12345", "9999999999", "notes.txt"]

        uid = detect_user_id_from_studio(environ, exists=exists, listdir=listdir)
        self.assertEqual(uid, "9999999999")

    def test_detect_watch_directories_from_conf_and_env(self):
        environ = {
            "APPDATA": r"C:\Users\demo\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\demo\AppData\Local",
        }
        conf_path = Path(r"C:\Users\demo\AppData\Roaming\BambuStudio\BambuStudio.conf")
        conf = {
            "app": {
                "download_path": r"D:\Downloads",
                "last_export_path": r"E:\prints",
            },
            "recent": {"last_opened_folder": r"E:\prints\recent"},
        }

        existing = {
            r"D:\Downloads",
            r"E:\prints",
            r"E:\prints\recent",
            r"C:\Users\demo\AppData\Local\Temp\bamboo_model",
            r"C:\Users\demo\AppData\Roaming\BambuStudio\cache",
        }

        def exists(p: Path) -> bool:
            key = str(p).replace("/", "\\")
            return key in existing

        def read_text(p: Path) -> str:
            self.assertEqual(str(p).replace("/", "\\"), str(conf_path).replace("/", "\\"))
            return json.dumps(conf)

        dirs = detect_watch_directories(environ, exists=exists, read_text=read_text)
        self.assertIn(r"D:\Downloads", dirs)
        self.assertIn(r"E:\prints", dirs)
        self.assertIn(r"E:\prints\recent", dirs)
        self.assertIn(r"C:\Users\demo\AppData\Local\Temp\bamboo_model", dirs)
        self.assertIn(r"C:\Users\demo\AppData\Roaming\BambuStudio\cache", dirs)

    def test_detect_device_id_from_conf_key(self):
        environ = {"APPDATA": r"C:\Users\demo\AppData\Roaming"}
        conf = {"user_last_selected_machine": "01S00A123456789"}

        def read_text(p: Path) -> str:
            return json.dumps(conf)

        # Path existence not required for read_text injection
        with mock.patch(
            "bambu_ams_check.autoconfig.studio_conf_path",
            return_value=Path(r"C:\Users\demo\AppData\Roaming\BambuStudio\BambuStudio.conf"),
        ):
            did = detect_device_id(environ, read_text=read_text)
        self.assertEqual(did, "01S00A123456789")

    def test_run_autoconfig_writes_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            # Minimal example seed
            (Path(tmp) / "config.example.yaml").write_text(
                "region: us\ncloud:\n  access_token: ''\n  user_id: ''\n",
                encoding="utf-8",
            )
            environ = {
                "APPDATA": str(Path(tmp) / "Roaming"),
                "LOCALAPPDATA": str(Path(tmp) / "Local"),
            }
            user_dir = Path(tmp) / "Roaming" / "BambuStudio" / "user" / "424242"
            user_dir.mkdir(parents=True)
            cache = Path(tmp) / "Roaming" / "BambuStudio" / "cache"
            cache.mkdir(parents=True)
            bamboo = Path(tmp) / "Local" / "Temp" / "bamboo_model"
            bamboo.mkdir(parents=True)
            conf_path = Path(tmp) / "Roaming" / "BambuStudio" / "BambuStudio.conf"
            conf_path.write_text(
                json.dumps(
                    {
                        "app": {"download_path": str(cache)},
                        "recent": {"last_opened_folder": str(bamboo)},
                        "user_last_selected_machine": "DEV123",
                    }
                ),
                encoding="utf-8",
            )

            # ensure_local_config_file looks for example in cwd — chdir
            with mock.patch("bambu_ams_check.config.Path.cwd", return_value=Path(tmp)):
                with mock.patch(
                    "bambu_ams_check.config.project_root", return_value=Path(tmp)
                ):
                    result = run_autoconfig(
                        config_path=path,
                        region="cn",
                        environ=environ,
                        launch_login=False,
                    )
            self.assertEqual(result.region, "cn")
            self.assertEqual(result.user_id, "424242")
            self.assertEqual(result.device_id, "DEV123")
            self.assertTrue(result.needs_login)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data["region"], "cn")
            self.assertEqual(data["cloud"]["user_id"], "424242")
            self.assertFalse(str(data["cloud"].get("access_token") or "").strip())
            self.assertIn(str(cache), data["watch"]["directories"])
            self.assertEqual(data["device"]["device_id"], "DEV123")


class TestDefaults(unittest.TestCase):
    def test_defaults_region_cn(self):
        self.assertEqual(DEFAULTS["region"], "cn")


if __name__ == "__main__":
    unittest.main()


class TestFirstRunAndReset(unittest.TestCase):
    def test_needs_first_run_when_local_missing(self):
        from bambu_ams_check.config import needs_first_run_autoconfig

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config.local.yaml"
            self.assertTrue(needs_first_run_autoconfig(missing))

    def test_first_run_ensure_writes_local(self):
        from bambu_ams_check.autoconfig import ensure_first_run_autoconfig
        from bambu_ams_check.config import needs_first_run_autoconfig

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            environ = {
                "APPDATA": str(Path(tmp) / "Roaming"),
                "LOCALAPPDATA": str(Path(tmp) / "Local"),
            }
            user_dir = Path(tmp) / "Roaming" / "BambuStudio" / "user" / "777"
            user_dir.mkdir(parents=True)
            cache = Path(tmp) / "Roaming" / "BambuStudio" / "cache"
            cache.mkdir(parents=True)
            conf = Path(tmp) / "Roaming" / "BambuStudio" / "BambuStudio.conf"
            conf.write_text(
                json.dumps({"app": {"download_path": str(cache)}}),
                encoding="utf-8",
            )
            self.assertTrue(needs_first_run_autoconfig(path))
            with mock.patch("bambu_ams_check.config.Path.cwd", return_value=Path(tmp)):
                with mock.patch(
                    "bambu_ams_check.config.project_root", return_value=Path(tmp)
                ):
                    result = ensure_first_run_autoconfig(
                        config_path=path,
                        region="cn",
                        environ=environ,
                    )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result.first_run)
            self.assertTrue(path.is_file())
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertTrue(data.get("autoconfig", {}).get("done"))
            self.assertEqual(data["cloud"]["user_id"], "777")
            self.assertIn(str(cache), data["watch"]["directories"])
            # Second call should skip
            again = ensure_first_run_autoconfig(config_path=path, environ=environ)
            self.assertIsNone(again)
            self.assertFalse(needs_first_run_autoconfig(path))

    def test_reset_clears_and_redetects(self):
        from bambu_ams_check.autoconfig import reset_config, run_autoconfig

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            (Path(tmp) / "config.example.yaml").write_text(
                "region: cn\ncloud:\n  access_token: ''\n  user_id: ''\n",
                encoding="utf-8",
            )
            environ_a = {
                "APPDATA": str(Path(tmp) / "A" / "Roaming"),
                "LOCALAPPDATA": str(Path(tmp) / "A" / "Local"),
            }
            environ_b = {
                "APPDATA": str(Path(tmp) / "B" / "Roaming"),
                "LOCALAPPDATA": str(Path(tmp) / "B" / "Local"),
            }

            def _seed(environ: dict[str, str], uid: str, folder: str) -> Path:
                user = Path(environ["APPDATA"]) / "BambuStudio" / "user" / uid
                user.mkdir(parents=True)
                watch = Path(environ["APPDATA"]) / "BambuStudio" / "cache" / folder
                watch.mkdir(parents=True)
                conf = Path(environ["APPDATA"]) / "BambuStudio" / "BambuStudio.conf"
                conf.write_text(
                    json.dumps(
                        {
                            "app": {"download_path": str(watch)},
                            "user_last_selected_machine": f"DEV-{uid}",
                        }
                    ),
                    encoding="utf-8",
                )
                return watch

            watch_a = _seed(environ_a, "111", "old")
            with mock.patch("bambu_ams_check.config.Path.cwd", return_value=Path(tmp)):
                with mock.patch(
                    "bambu_ams_check.config.project_root", return_value=Path(tmp)
                ):
                    run_autoconfig(config_path=path, region="cn", environ=environ_a)
            # Simulate login tokens
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["cloud"]["access_token"] = "keep-me-token-value-12345678"
            data["cloud"]["user_id"] = "111"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            self.assertIn(str(watch_a), data["watch"]["directories"])

            watch_b = _seed(environ_b, "222", "new")
            with mock.patch("bambu_ams_check.config.Path.cwd", return_value=Path(tmp)):
                with mock.patch(
                    "bambu_ams_check.config.project_root", return_value=Path(tmp)
                ):
                    result = reset_config(
                        config_path=path,
                        keep_token=True,
                        region="cn",
                        environ=environ_b,
                    )
            self.assertTrue(result.reset)
            fresh = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(fresh["cloud"]["access_token"], "keep-me-token-value-12345678")
            # Re-detected from environ_b
            self.assertEqual(fresh["cloud"]["user_id"], "222")
            self.assertIn(str(watch_b), fresh["watch"]["directories"])
            self.assertNotIn(str(watch_a), fresh["watch"]["directories"])
            self.assertEqual(fresh["device"]["device_id"], "DEV-222")
            self.assertTrue(fresh.get("autoconfig", {}).get("done"))

    def test_reset_without_keep_token_drops_secret(self):
        from bambu_ams_check.autoconfig import reset_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "region": "cn",
                        "cloud": {
                            "access_token": "secret-token-abcdefgh",
                            "user_id": "1",
                        },
                        "watch": {"directories": ["D:/old"]},
                        "autoconfig": {"done": True},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            environ = {
                "APPDATA": str(Path(tmp) / "Roaming"),
                "LOCALAPPDATA": str(Path(tmp) / "Local"),
            }
            (Path(tmp) / "Roaming" / "BambuStudio" / "user" / "9").mkdir(parents=True)
            with mock.patch("bambu_ams_check.config.Path.cwd", return_value=Path(tmp)):
                with mock.patch(
                    "bambu_ams_check.config.project_root", return_value=Path(tmp)
                ):
                    reset_config(
                        config_path=path,
                        keep_token=False,
                        environ=environ,
                    )
            fresh = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertFalse(str(fresh.get("cloud", {}).get("access_token") or "").strip())


class TestReadStudioConf(unittest.TestCase):
    def test_read_studio_conf_with_md5_trailer(self):
        from bambu_ams_check.autoconfig import _read_studio_conf
        raw = (
            "{\n"
            "  \"app\": {\"download_path\": \"C:\\\\Users\\\\A\\\\Downloads\", "
            "\"user_last_selected_machine\": \"DEV123\"},\n"
            "  \"access_code\": {\"DEV123\": \"abcd\"}\n"
            "}\n"
            "# MD5 checksum DEADBEEF\n"
        )
        data = _read_studio_conf(Path("dummy"), read_text=lambda _p: raw)
        self.assertEqual(data["app"]["download_path"], r"C:\Users\A\Downloads")
        self.assertEqual(data["app"]["user_last_selected_machine"], "DEV123")
