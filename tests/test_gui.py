"""Tests for the config writer and the Tkinter UI."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wheresmygpu.config import (
    AlertRule,
    AppSettings,
    Config,
    ServerConfig,
    SshSettings,
    load_config,
)
from wheresmygpu.config_writer import (
    dump_config,
    find_channel,
    has_channel,
    remove_channel,
    upsert_channel,
)


def _sample_config(path: Path) -> Config:
    return Config(
        path=path,
        app=AppSettings(interval_seconds=120),
        ssh=SshSettings(),
        alert=AlertRule(min_free_memory_gb=18.0, sustained_checks=3, cooldown_minutes=15),
        servers=[
            ServerConfig(
                name="s1", host="10.0.0.1", user="ubuntu", port=2222,
                label="测试一号", password="pw1",
            ),
            ServerConfig(name="s2", host="10.0.0.2", user="root", key_file="/k/id_rsa"),
        ],
        channels=[
            {
                "type": "email",
                "smtp_host": "smtp.qq.com",
                "smtp_port": 465,
                "use_ssl": True,
                "username": "a@qq.com",
                "password": "authcode",
                "to": ["a@qq.com", "b@x.com"],
            },
            {"type": "logfile", "path": "logs/alerts.log"},
        ],
    )


class ConfigWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.yaml"

    def test_round_trip_preserves_everything(self) -> None:
        dump_config(_sample_config(self.path), self.path)
        back = load_config(self.path)

        self.assertEqual(back.app.interval_seconds, 120)
        self.assertEqual(back.alert.min_free_memory_gb, 18.0)
        self.assertEqual(back.alert.sustained_checks, 3)
        self.assertEqual(back.alert.cooldown_minutes, 15)

        self.assertEqual([s.name for s in back.servers], ["s1", "s2"])
        self.assertEqual(back.servers[0].password, "pw1")
        self.assertEqual(back.servers[0].port, 2222)
        self.assertEqual(back.servers[1].key_file, "/k/id_rsa")

        email = find_channel(back.channels, "email")
        self.assertIsNotNone(email)
        assert email is not None
        self.assertEqual(email["smtp_host"], "smtp.qq.com")
        self.assertEqual(email["smtp_port"], 465)
        self.assertEqual(email["to"], ["a@qq.com", "b@x.com"])
        self.assertEqual(email["password"], "authcode")
        self.assertTrue(has_channel(back.channels, "logfile"))

    def test_dump_keeps_a_backup_of_the_previous_file(self) -> None:
        config = _sample_config(self.path)
        dump_config(config, self.path)
        first = self.path.read_text(encoding="utf-8")

        config.app.interval_seconds = 999
        dump_config(config, self.path)

        backup = self.path.with_suffix(self.path.suffix + ".bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), first)
        self.assertIn("999", self.path.read_text(encoding="utf-8"))

    def test_written_file_is_utf8_and_keeps_chinese(self) -> None:
        config = _sample_config(self.path)
        config.servers[0].label = "服务器一号"
        config.servers[1].label = "服务器二号"
        dump_config(config, self.path)

        raw = self.path.read_bytes()
        self.assertIn("服务器一号", raw.decode("utf-8"))
        self.assertEqual(load_config(self.path).servers[0].label, "服务器一号")

    def test_no_temporary_file_is_left_behind(self) -> None:
        dump_config(_sample_config(self.path), self.path)
        leftovers = [p.name for p in self.tmp.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_channel_helpers(self) -> None:
        channels = [{"type": "logfile"}, {"type": "email", "smtp_host": "x"}]
        self.assertTrue(has_channel(channels, "email"))
        self.assertFalse(has_channel(channels, "sound"))

        updated = upsert_channel(channels, {"type": "email", "smtp_host": "y"})
        self.assertEqual(len(updated), 2)
        self.assertEqual(find_channel(updated, "email")["smtp_host"], "y")
        self.assertEqual(find_channel(channels, "email")["smtp_host"], "x")

        added = upsert_channel(channels, {"type": "sound"})
        self.assertEqual(len(added), 3)
        self.assertEqual(
            [c["type"] for c in remove_channel(added, "email")], ["logfile", "sound"]
        )

    def test_explicitly_disabled_channel_counts_as_absent(self) -> None:
        self.assertFalse(has_channel([{"type": "email", "enabled": False}], "email"))
        self.assertTrue(has_channel([{"type": "email", "enabled": True}], "email"))


class GuiTests(unittest.TestCase):
    """Build the real window and drive it programmatically."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tkinter

            root = tkinter.Tk()
            root.destroy()
        except Exception as exc:  # pragma: no cover - depends on the machine
            raise unittest.SkipTest(f"Tk is not available here: {exc}") from exc

    def setUp(self) -> None:
        from wheresmygpu.gui import WheresMyGPUApp

        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.yaml"
        dump_config(_sample_config(self.path), self.path)
        self.app = WheresMyGPUApp(self.path)
        self.app.update()

    def tearDown(self) -> None:
        try:
            self.app.destroy()
        except Exception:
            pass

    # -- layout ---------------------------------------------------------- #

    def test_all_tabs_are_present(self) -> None:
        names = [
            self.app.notebook.tab(tab, "text").strip()
            for tab in self.app.notebook.tabs()
        ]
        self.assertEqual(
            names, ["监控状态", "服务器", "阈值设置", "邮件提醒", "运行日志"]
        )

    def test_threshold_inputs_are_populated_from_config(self) -> None:
        self.assertEqual(self.app.var_interval.get(), "120")
        self.assertEqual(self.app.var_min_free_gb.get(), "18")
        self.assertTrue(self.app.var_min_free_enabled.get())
        self.assertEqual(self.app.var_sustained.get(), "3")
        self.assertEqual(self.app.var_cooldown.get(), "15")
        self.assertEqual(self.app.var_max_util.get(), "5")

    def test_servers_are_listed(self) -> None:
        self.assertEqual(len(self.app.server_tree.get_children()), 2)

    def test_email_fields_are_populated_from_config(self) -> None:
        self.assertTrue(self.app.var_email_enabled.get())
        self.assertEqual(self.app.var_smtp_host.get(), "smtp.qq.com")
        self.assertEqual(self.app.var_smtp_port.get(), "465")
        self.assertEqual(self.app.var_smtp_user.get(), "a@qq.com")

    # -- editing --------------------------------------------------------- #

    def test_edited_thresholds_round_trip_through_disk(self) -> None:
        self.app.var_min_free_gb.set("12")
        self.app.var_sustained.set("4")
        self.app.var_cooldown.set("30")

        collected = self.app._collect_config()
        self.assertEqual(collected.alert.min_free_memory_gb, 12.0)
        self.assertEqual(collected.alert.sustained_checks, 4)
        self.assertEqual(collected.alert.cooldown_minutes, 30)

        out = self.tmp / "out.yaml"
        dump_config(collected, out)
        back = load_config(out)
        self.assertEqual(back.alert.min_free_memory_gb, 12.0)
        self.assertEqual(back.alert.sustained_checks, 4)
        self.assertEqual(back.alert.cooldown_minutes, 30)

    def test_disabling_a_threshold_turns_it_off(self) -> None:
        self.app.var_max_temp_enabled.set(True)
        self.app.var_max_temp.set("70")
        self.assertEqual(self.app._collect_config().alert.max_temperature_c, 70.0)

        self.app.var_max_temp_enabled.set(False)
        self.assertIsNone(self.app._collect_config().alert.max_temperature_c)

    def test_interval_below_minimum_is_rejected(self) -> None:
        self.app.var_interval.set("3")
        with self.assertRaises(ValueError):
            self.app._collect_config()

    def test_non_numeric_threshold_is_rejected(self) -> None:
        self.app.var_min_free_gb.set("二十")
        with self.assertRaises(ValueError):
            self.app._collect_config()

    # -- validation ------------------------------------------------------ #

    def test_email_can_be_disabled(self) -> None:
        self.app.var_email_enabled.set(False)
        kinds = [c["type"] for c in self.app._collect_config().channels]
        self.assertNotIn("email", kinds)

    def test_incomplete_email_is_refused_but_preview_survives(self) -> None:
        self.app.var_email_enabled.set(True)
        self.app.var_smtp_host.set("")
        with self.assertRaises(ValueError):
            self.app._collect_config()

        relaxed = self.app._collect_config(validate_email=False)
        self.assertIn("email", [c["type"] for c in relaxed.channels])

    def test_email_spec_is_built_from_the_input_areas(self) -> None:
        self.app.var_smtp_host.set("smtp.163.com")
        self.app.var_smtp_port.set("465")
        self.app.var_smtp_user.set("me@163.com")
        self.app.var_smtp_password.set("code123")
        self.app.var_smtp_to.set("a@x.com, b@y.com; c@z.com")
        self.app.var_smtp_from.set("me@163.com")

        spec = self.app._collect_email_spec()
        self.assertEqual(spec["smtp_host"], "smtp.163.com")
        self.assertEqual(spec["smtp_port"], 465)
        self.assertEqual(spec["to"], ["a@x.com", "b@y.com", "c@z.com"])
        self.assertEqual(spec["from"], "me@163.com")
        self.assertEqual(spec["username"], "me@163.com")
        self.assertTrue(spec["use_ssl"])

    def test_single_recipient_is_stored_as_a_string(self) -> None:
        self.app.var_smtp_to.set("only@x.com")
        self.assertEqual(self.app._collect_email_spec()["to"], "only@x.com")

    def test_bad_port_is_rejected(self) -> None:
        self.app.var_smtp_port.set("70000")
        with self.assertRaises(ValueError):
            self.app._collect_email_spec()

    def test_rule_preview_warns_about_an_impossible_threshold(self) -> None:
        self.app.var_min_free_gb.set("24")
        self.app.var_min_free_enabled.set(True)
        self.app._refresh_rule_preview()
        warning = self.app.lbl_rule_warn.cget("text")
        self.assertIn("24", warning)
        self.assertIn("23.6", warning)

    def test_rule_preview_describes_the_current_rule(self) -> None:
        self.app.var_max_util.set("3")
        self.app._refresh_rule_preview()
        self.assertIn("GPU util <= 3%", self.app.lbl_rule.cget("text"))

    # -- email helpers --------------------------------------------------- #

    def test_email_preset_fills_host_port_and_encryption(self) -> None:
        self.app.var_preset.set("QQ 邮箱 (smtp.qq.com)")
        self.app._apply_email_preset()
        self.assertEqual(self.app.var_smtp_host.get(), "smtp.qq.com")
        self.assertEqual(self.app.var_smtp_port.get(), "465")
        self.assertTrue(self.app.var_smtp_ssl.get())

        self.app.var_preset.set("Outlook (smtp.office365.com)")
        self.app._apply_email_preset()
        self.assertEqual(self.app.var_smtp_port.get(), "587")
        self.assertFalse(self.app.var_smtp_ssl.get())
        self.assertTrue(self.app.var_smtp_starttls.get())

    def test_extra_channels_follow_their_checkboxes(self) -> None:
        self.app.channel_vars["windows_toast"].set(True)
        self.assertIn(
            "windows_toast", [c["type"] for c in self.app._collect_config().channels]
        )
        self.app.channel_vars["windows_toast"].set(False)
        self.assertNotIn(
            "windows_toast", [c["type"] for c in self.app._collect_config().channels]
        )

    def test_logfile_channel_keeps_its_path(self) -> None:
        self.app.channel_vars["logfile"].set(True)
        spec = find_channel(self.app._collect_config().channels, "logfile")
        self.assertEqual(spec["path"], "logs/alerts.log")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()