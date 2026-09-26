"""Tests for full-trace campaign purge: static artifacts, Gmail drafts, exports.

No Firestore, network, or git access: external modules are faked in
sys.modules and the filesystem is a temporary directory.
"""
import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.routes.scrape import (
    _delete_campaign_export_bundles,
    _delete_campaign_static_paths,
    _delete_gmail_draft,
)


def _static_fakes(tmp, push):
    gen = types.ModuleType("app.services.static_website_generator")
    gen.websites_root = lambda: tmp / "sites"
    gen.git_commit_and_push_static_paths = push
    art = types.ModuleType("app.services.audit_report_artifacts")
    art.audit_reports_root = lambda: tmp / "audits"
    return {
        "app.services.static_website_generator": gen,
        "app.services.audit_report_artifacts": art,
    }


class DeleteCampaignStaticPathsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def _write(self, *parts, content="x"):
        path = self.tmp.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_removes_artifacts_and_pushes(self):
        rep = self._write("audits", "rep1", "index.html")
        site = self._write("sites", "lead1", "index.html")
        outside = self._write("other", "x")
        push = MagicMock(return_value={"committed": True, "pushed": True})
        leads = [
            {"id": "l1", "generated_website_path": str(site.parent)},
            {"id": "l2", "generated_website_path": str(outside)},
            {"id": "l3", "generated_website_path": str(self.tmp / "sites" / "gone")},
        ]
        reports = [("audit_reports", {"id": "r1", "static_report_path": str(rep.parent)})]
        with patch.dict(sys.modules, _static_fakes(self.tmp, push)):
            _delete_campaign_static_paths("c1", leads, reports)
        self.assertFalse(rep.parent.exists())
        self.assertFalse(site.parent.exists())
        self.assertTrue(outside.exists())
        push.assert_called_once()
        paths, kwargs = push.call_args[0][0], push.call_args[1]
        self.assertEqual(len(paths), 2)
        self.assertIn("c1", kwargs["commit_message"])

    def test_missing_modules_do_not_raise(self):
        bare_gen = types.ModuleType("app.services.static_website_generator")
        bare_art = types.ModuleType("app.services.audit_report_artifacts")
        with patch.dict(
            sys.modules,
            {
                "app.services.static_website_generator": bare_gen,
                "app.services.audit_report_artifacts": bare_art,
            },
        ):
            _delete_campaign_static_paths("c1", [{"id": "l1"}], [])

    def test_push_failure_does_not_raise(self):
        self._write("sites", "lead1", "index.html")
        push = MagicMock(side_effect=RuntimeError("git down"))
        leads = [
            {
                "id": "l1",
                "generated_website_path": str(self.tmp / "sites" / "lead1"),
            }
        ]
        with patch.dict(sys.modules, _static_fakes(self.tmp, push)):
            _delete_campaign_static_paths("c1", leads, [])
        self.assertFalse((self.tmp / "sites" / "lead1").exists())


class DeleteGmailDraftTests(unittest.TestCase):
    def _run(self, draft, deleter):
        mod = types.ModuleType("app.services.gmail_service")
        mod.delete_gmail_draft = deleter
        with patch.dict(sys.modules, {"app.services.gmail_service": mod}):
            _delete_gmail_draft(draft)

    def test_deletes_when_id_present(self):
        deleter = MagicMock()
        self._run({"id": "d1", "gmail_draft_id": "g1"}, deleter)
        deleter.assert_called_once_with("g1")

    def test_skips_without_id(self):
        deleter = MagicMock()
        self._run({"id": "d1"}, deleter)
        deleter.assert_not_called()

    def test_failure_does_not_raise(self):
        deleter = MagicMock(side_effect=RuntimeError("gmail down"))
        self._run({"id": "d1", "gmail_draft_id": "g1"}, deleter)


class DeleteCampaignExportBundlesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.settings = SimpleNamespace(export_dir=str(self.tmp))

    def _zip(self, name, campaign_ids=None, corrupt=False):
        path = self.tmp / name
        if corrupt:
            path.write_bytes(b"not a zip")
            return path
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w") as archive:
            archive.writestr(
                "all-campaigns-summary.json",
                json.dumps({"campaigns": [{"id": cid} for cid in (campaign_ids or [])]}),
            )
        path.write_bytes(buf.getvalue())
        return path

    def _run(self, campaign_id):
        with patch("app.routes.scrape.get_settings", return_value=self.settings):
            _delete_campaign_export_bundles(campaign_id)

    def test_removes_matching_bundle_and_status(self):
        self._zip("campaign-export-e1.zip", ["c1", "c2"])
        (self.tmp / "e1.json").write_text("{}", encoding="utf-8")
        self._zip("campaign-export-e2.zip", ["c2"])
        (self.tmp / "e2.json").write_text("{}", encoding="utf-8")
        self._run("c1")
        self.assertFalse((self.tmp / "campaign-export-e1.zip").exists())
        self.assertFalse((self.tmp / "e1.json").exists())
        self.assertTrue((self.tmp / "campaign-export-e2.zip").exists())
        self.assertTrue((self.tmp / "e2.json").exists())

    def test_corrupt_and_missing_dir_do_not_raise(self):
        self._zip("campaign-export-bad.zip", corrupt=True)
        self._run("c1")
        self.assertTrue((self.tmp / "campaign-export-bad.zip").exists())
        self.settings = SimpleNamespace(export_dir=str(self.tmp / "nope"))
        self._run("c1")


if __name__ == "__main__":
    unittest.main()
