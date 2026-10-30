"""API + pipeline tests with mocked providers — no real API credits used.
Run:  python -m unittest discover tests -v
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test_leads.db")

import app as webapp
import backup
import jobs
import lead_pipeline as lp
import linkedin
import outreach
import settings_store
import sheets
import throttle


def reset_db():
    # worker thread kabhi-kabhi connection hold karta hai (Windows file lock)
    for _ in range(50):
        try:
            if os.path.exists(lp.DB_PATH):
                os.remove(lp.DB_PATH)
            return
        except PermissionError:
            time.sleep(0.1)
    raise RuntimeError("DB reset nahi ho paya")


def wait_job(client, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get(f"/api/jobs/{job_id}").json
        if j.get("status") in ("done", "paused"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} timeout")


class BaseTest(unittest.TestCase):
    def setUp(self):
        reset_db()
        for env in webapp.KEY_ENVS + ["APP_PASSWORD", "WEBHOOK_URL", "PUSH_VALID", "SECRET_KEY",
                                      "OUTREACH_PROVIDER", "OUTREACH_API_KEY", "OUTREACH_CAMPAIGN",
                                      "GOOGLE_SERVICE_ACCOUNT_JSON"]:
            os.environ.pop(env, None)
        os.environ["PATTERN_GUESS"] = "0"
        for p in ("HUNTER", "PROSPEO", "APOLLO"):
            os.environ.pop(f"{p}_LIMIT", None)
        webapp._login_fails.clear()
        self.client = webapp.app.test_client()

    def set_fake_keys(self):
        os.environ["REOON_API_KEY"] = "fake"
        os.environ["HUNTER_API_KEY"] = "fake"


class ApiTest(BaseTest):
    def test_settings_masks_keys(self):
        os.environ["REOON_API_KEY"] = "secret-value"
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["keys"]["REOON_API_KEY"])
        self.assertNotIn("secret-value", r.get_data(as_text=True))

    def test_settings_save(self):
        r = self.client.post("/api/settings", json={
            "keys": {"REOON_API_KEY": "abc"}, "limits": {"hunter": 10}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(os.environ["REOON_API_KEY"], "abc")
        self.assertEqual(os.environ["HUNTER_LIMIT"], "10")

    def test_settings_bad_limit(self):
        r = self.client.post("/api/settings", json={"limits": {"hunter": "oops"}})
        self.assertEqual(r.status_code, 400)

    def test_settings_flags_and_webhook(self):
        r = self.client.post("/api/settings", json={
            "flags": {"pattern_guess": True, "push_valid": True},
            "webhook_url": "https://hooks.example.com/x"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["flags"]["pattern_guess"])
        self.assertTrue(r.json["flags"]["push_valid"])
        self.assertTrue(r.json["webhook_set"])
        self.assertNotIn("hooks.example.com", r.get_data(as_text=True))
        os.environ.pop("PATTERN_GUESS", None)
        settings_store.load_into_env()
        self.assertEqual(os.environ["PATTERN_GUESS"], "1")
        self.assertEqual(os.environ["WEBHOOK_URL"], "https://hooks.example.com/x")

    def test_settings_bad_webhook(self):
        r = self.client.post("/api/settings", json={"webhook_url": "ftp://nope"})
        self.assertEqual(r.status_code, 400)

    def test_find_requires_fields(self):
        r = self.client.post("/api/find", json={"first_name": "A"})
        self.assertEqual(r.status_code, 400)

    def test_find_requires_keys(self):
        r = self.client.post("/api/find", json={
            "first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Reoon", r.json["error"])

    def test_find_happy_and_cache(self):
        self.set_fake_keys()
        with mock.patch.object(lp, "find_email", return_value=("p@stripe.com", "valid", "hunter")) as m:
            r = self.client.post("/api/find", json={
                "first_name": "Patrick", "last_name": "Collison", "domain": "https://www.stripe.com/"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json["email"], "p@stripe.com")
            self.assertEqual(r.json["status"], "valid")
            self.assertEqual(m.call_count, 1)
            self.assertEqual(m.call_args.args[3], "stripe.com")  # domain cleaned
            r2 = self.client.post("/api/find", json={
                "first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
            self.assertEqual(m.call_count, 1)  # cached, no second lookup
            self.assertIn("Pehle se", r2.json["log"])
        hist = self.client.get("/api/history").json
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["email"], "p@stripe.com")

    def test_pattern_guess_skips_paid_providers(self):
        self.set_fake_keys()
        os.environ["PATTERN_GUESS"] = "1"
        with mock.patch.object(lp, "verify", return_value="valid") as v, \
             mock.patch.object(lp, "find_email") as finder:
            r = self.client.post("/api/find", json={
                "first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
            self.assertEqual(r.json["email"], "patrick.collison@stripe.com")
            self.assertEqual(r.json["source"], "pattern")
            finder.assert_not_called()  # credits bache
            self.assertEqual(v.call_count, 1)  # pehla guess hi valid

    def test_pattern_risky_then_provider_valid(self):
        self.set_fake_keys()
        os.environ["PATTERN_GUESS"] = "1"
        with mock.patch.object(lp, "verify", return_value="catch_all"), \
             mock.patch.object(lp, "find_email", return_value=("real@stripe.com", "valid", "hunter")):
            r = self.client.post("/api/find", json={
                "first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
            self.assertEqual(r.json["email"], "real@stripe.com")
            self.assertEqual(r.json["status"], "valid")

    def test_webhook_auto_push_on_valid(self):
        self.set_fake_keys()
        os.environ["PUSH_VALID"] = "1"
        os.environ["WEBHOOK_URL"] = "https://hooks.example.com/x"
        with mock.patch.object(lp, "find_email", return_value=("p@stripe.com", "valid", "hunter")), \
             mock.patch.object(webapp.requests, "post") as post:
            post.return_value.ok = True
            post.return_value.status_code = 200
            self.client.post("/api/find", json={
                "first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
            post.assert_called_once()
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["email"], "p@stripe.com")
            self.assertEqual(payload["event"], "lead.valid")

    def test_manual_push(self):
        self.set_fake_keys()
        os.environ["WEBHOOK_URL"] = "https://hooks.example.com/x"
        with mock.patch.object(lp, "find_email", return_value=("p@stripe.com", "valid", "hunter")):
            self.client.post("/api/find", json={
                "first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
        with mock.patch.object(webapp.requests, "post") as post:
            post.return_value.ok = True
            post.return_value.status_code = 200
            r = self.client.post("/api/push", json={})
            self.assertEqual(r.json, {"pushed": 1, "failed": 0, "total": 1})

    def test_quota_alerts_flags(self):
        os.environ["HUNTER_LIMIT"] = "2"
        r = self.client.get("/api/quota")
        self.assertEqual(r.status_code, 200)
        hunter = next(q for q in r.json if q["provider"] == "hunter")
        self.assertEqual(hunter["remaining"], 2)
        self.assertTrue(hunter["low"])  # <= max(2, 10%)
        self.assertFalse(hunter["exhausted"])

    def test_stats(self):
        self.set_fake_keys()
        with mock.patch.object(lp, "find_email", side_effect=[
            ("a@x.com", "valid", "hunter"), (None, "not_found", None)]):
            self.client.post("/api/find", json={"first_name": "A", "last_name": "B", "domain": "x.com"})
            self.client.post("/api/find", json={"first_name": "C", "last_name": "D", "domain": "y.com"})
        r = self.client.get("/api/stats")
        self.assertEqual(r.json["status_counts"], {"valid": 1, "not_found": 1})
        self.assertEqual(len(r.json["daily"]), 2)

    def test_concurrent_quota_and_history_on_fresh_db(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self.client.get, "/api/quota")
            f2 = pool.submit(self.client.get, "/api/history")
            self.assertEqual(f1.result().status_code, 200)
            self.assertEqual(f2.result().status_code, 200)


class JobsTest(BaseTest):
    def upload(self, csv_bytes, name="leads.csv"):
        return self.client.post("/api/jobs", data={"file": (io.BytesIO(csv_bytes), name)})

    def test_job_missing_columns(self):
        self.set_fake_keys()
        r = self.upload(b"name,company\nX,Y\n")
        self.assertEqual(r.status_code, 400)
        self.assertIn("missing", r.json["error"])

    def test_job_runs_in_background(self):
        self.set_fake_keys()
        csv_data = (b"first_name,last_name,domain,company\n"
                    b"Patrick,Collison,stripe.com,Stripe\n"
                    b"John,Doe,example.com,Example\n")
        with mock.patch.object(lp, "find_email", side_effect=[
            ("p@stripe.com", "valid", "hunter"),
            (None, "not_found", None),
        ]):
            r = self.upload(csv_data)
            self.assertEqual(r.status_code, 200)
            job = wait_job(self.client, r.json["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["done"], 2)
        rows = job["rows"]
        self.assertEqual([x["status"] for x in rows], ["valid", "not_found"])
        self.assertEqual(rows[0]["company"], "Stripe")  # extra CSV column preserved
        listing = self.client.get("/api/jobs").json
        self.assertEqual(listing[0]["id"], r.json["job_id"])

    def test_job_failed_row_then_retry(self):
        self.set_fake_keys()
        csv_data = b"first_name,last_name,domain\nA,B,x.com\n"
        with mock.patch.object(lp, "find_email", side_effect=RuntimeError("network boom")):
            r = self.upload(csv_data)
            job = wait_job(self.client, r.json["job_id"])
        self.assertEqual(job["rows"][0]["state"], "failed")
        self.assertIn("network boom", job["rows"][0]["log"])
        with mock.patch.object(lp, "find_email", return_value=("a@x.com", "valid", "hunter")):
            rr = self.client.post(f"/api/jobs/{r.json['job_id']}/resume", json={})
            self.assertEqual(rr.status_code, 200)
            job = wait_job(self.client, r.json["job_id"])
        self.assertEqual(job["rows"][0]["state"], "done")
        self.assertEqual(job["rows"][0]["email"], "a@x.com")

    def test_job_auto_pause_and_resume(self):
        self.set_fake_keys()
        os.environ["HUNTER_LIMIT"] = "0"  # sab credits khatam, pattern off
        r = self.upload(b"first_name,last_name,domain\nA,B,x.com\n")
        job = wait_job(self.client, r.json["job_id"])
        self.assertEqual(job["status"], "paused")
        self.assertIn("credits khatam", job["error"])
        self.assertEqual(job["rows"][0]["state"], "pending")  # kuch kharch nahi hua
        os.environ["HUNTER_LIMIT"] = "5"
        with mock.patch.object(lp, "find_email", return_value=("a@x.com", "valid", "hunter")):
            self.client.post(f"/api/jobs/{r.json['job_id']}/resume", json={})
            job = wait_job(self.client, r.json["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["rows"][0]["email"], "a@x.com")

    def test_resume_after_restart(self):
        self.set_fake_keys()
        conn = lp.db()
        try:
            conn.executescript(jobs.SCHEMA)
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                         ("restartjob", "batch", "running", 1, "2026-10-30", "old.csv", None))
            conn.execute("INSERT INTO job_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("restartjob", 0, "A", "B", "x.com", "", "{}", "", "", "", "pending", ""))
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(lp, "find_email", return_value=("a@x.com", "valid", "hunter")):
            jobs.recover_interrupted()  # simulated redeploy
            job = wait_job(self.client, "restartjob")
        self.assertEqual(job["status"], "done")

    def test_reverify_single_invalidates(self):
        self.set_fake_keys()
        with mock.patch.object(lp, "find_email", return_value=("a@x.com", "valid", "hunter")):
            self.client.post("/api/find", json={"first_name": "A", "last_name": "B", "domain": "x.com"})
        with mock.patch.object(lp, "verify", return_value="invalid"):
            r = self.client.post("/api/reverify", json={
                "first_name": "A", "last_name": "B", "domain": "x.com"})
        self.assertEqual(r.json["status"], "not_found")
        self.assertEqual(r.json["email"], "")
        hist = self.client.get("/api/history").json
        self.assertEqual(hist[0]["status"], "not_found")

    def test_reverify_bulk_job(self):
        self.set_fake_keys()
        with mock.patch.object(lp, "find_email", return_value=("risky@x.com", "risky", "hunter")):
            self.client.post("/api/find", json={"first_name": "A", "last_name": "B", "domain": "x.com"})
        with mock.patch.object(lp, "verify", return_value="valid"):
            r = self.client.post("/api/reverify-bulk", json={"status": "risky"})
            self.assertEqual(r.status_code, 200)
            job = wait_job(self.client, r.json["job_id"])
        self.assertEqual(job["kind"], "reverify")
        self.assertEqual(job["rows"][0]["status"], "valid")
        hist = self.client.get("/api/history").json
        self.assertEqual(hist[0]["status"], "valid")  # leads table updated

    def test_history_filter_and_export(self):
        self.set_fake_keys()
        with mock.patch.object(lp, "find_email", side_effect=[
            ("a@x.com", "valid", "hunter"), (None, "not_found", None)]):
            self.client.post("/api/find", json={"first_name": "Alice", "last_name": "B", "domain": "x.com"})
            self.client.post("/api/find", json={"first_name": "Bob", "last_name": "C", "domain": "y.com"})
        r = self.client.get("/api/history?status=valid")
        self.assertEqual(len(r.json), 1)
        self.assertEqual(r.json[0]["first_name"], "Alice")
        r = self.client.get("/api/history?q=alice")
        self.assertEqual(len(r.json), 1)
        r = self.client.get("/api/history/export?status=valid")
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn("first_name,last_name,domain,email,status,source,found_at", text)
        self.assertIn("a@x.com", text)
        self.assertNotIn("Bob", text)


class AuthTest(unittest.TestCase):
    def setUp(self):
        reset_db()
        os.environ["APP_PASSWORD"] = "s3cret-pass"
        webapp._login_fails.clear()
        self.client = webapp.app.test_client()

    def tearDown(self):
        os.environ.pop("APP_PASSWORD", None)

    def test_healthz_public(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_api_requires_login(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.json["login"])

    def test_page_redirects_to_login(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_wrong_password(self):
        r = self.client.post("/api/login", json={"password": "galat"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_login_logout_flow(self):
        r = self.client.post("/api/login", json={"password": "s3cret-pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_no_password_means_dev_mode(self):
        os.environ.pop("APP_PASSWORD", None)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)


class SecurityTest(BaseTest):
    def test_encryption_at_rest(self):
        os.environ["SECRET_KEY"] = "test-secret-key"
        self.client.post("/api/settings", json={"keys": {"REOON_API_KEY": "super-secret"}})
        conn = lp.db()
        try:
            raw = conn.execute("SELECT value FROM settings WHERE key='REOON_API_KEY'").fetchone()["value"]
        finally:
            conn.close()
        self.assertTrue(raw.startswith("enc:"))
        self.assertNotIn("super-secret", raw)
        os.environ.pop("REOON_API_KEY", None)
        settings_store.load_into_env()
        self.assertEqual(os.environ["REOON_API_KEY"], "super-secret")

    def test_wrong_secret_key_does_not_leak_garbage(self):
        os.environ["SECRET_KEY"] = "key-one"
        settings_store.save({"REOON_API_KEY": "abc"})
        os.environ["SECRET_KEY"] = "key-two"
        os.environ.pop("REOON_API_KEY", None)
        settings_store.load_into_env()
        self.assertNotIn("REOON_API_KEY", os.environ)  # recover nahi hua, garbage nahi gaya

    def test_login_lockout(self):
        os.environ["APP_PASSWORD"] = "right-pass"
        for _ in range(webapp.LOGIN_MAX_FAILS):
            r = self.client.post("/api/login", json={"password": "wrong"})
            self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/login", json={"password": "right-pass"})
        self.assertEqual(r.status_code, 429)  # sahi password bhi lockout mein block
        webapp._login_fails.clear()
        r = self.client.post("/api/login", json={"password": "right-pass"})
        self.assertEqual(r.status_code, 200)

    def test_api_token_flow(self):
        os.environ["APP_PASSWORD"] = "pw"
        self.client.post("/api/login", json={"password": "pw"})
        r = self.client.post("/api/tokens", json={"name": "script"})
        token = r.json["token"]
        self.assertTrue(token.startswith("lf_"))
        listing = self.client.get("/api/tokens").json
        self.assertNotIn(token, str(listing))  # plaintext list mein nahi
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        r = self.client.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)  # token auth kaam karta hai
        r = self.client.get("/api/settings", headers={"Authorization": "Bearer lf_galat"})
        self.assertEqual(r.status_code, 401)
        token_id = listing[0]["id"]
        self.client.delete(f"/api/tokens/{token_id}", headers={"Authorization": f"Bearer {token}"})
        r = self.client.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 401)  # revoke ho gaya


class CrmTest(BaseTest):
    def seed_lead(self):
        self.set_fake_keys()
        with mock.patch.object(lp, "find_email", return_value=("a@x.com", "valid", "hunter")):
            self.client.post("/api/find", json={"first_name": "A", "last_name": "B", "domain": "x.com"})

    def test_meta_upsert_and_filter(self):
        self.seed_lead()
        r = self.client.post("/api/leads/meta", json={
            "first_name": "A", "last_name": "B", "domain": "x.com",
            "pipeline": "contacted", "tags": "saas,hot", "notes": "demo call done"})
        self.assertEqual(r.status_code, 200)
        hist = self.client.get("/api/history").json
        self.assertEqual(hist[0]["pipeline"], "contacted")
        self.assertEqual(hist[0]["tags"], "saas,hot")
        self.assertEqual(len(self.client.get("/api/history?pipeline=contacted").json), 1)
        self.assertEqual(len(self.client.get("/api/history?pipeline=replied").json), 0)
        self.assertEqual(len(self.client.get("/api/history?q=hot").json), 1)  # tags search
        stats = self.client.get("/api/stats").json
        self.assertEqual(stats["pipeline_counts"].get("contacted"), 1)
        export = self.client.get("/api/history/export").get_data(as_text=True)
        self.assertIn("contacted", export)

    def test_meta_bad_pipeline(self):
        r = self.client.post("/api/leads/meta", json={
            "first_name": "A", "last_name": "B", "domain": "x.com", "pipeline": "galat"})
        self.assertEqual(r.status_code, 400)


class LinkedInTest(BaseTest):
    def test_linkedin_lookup(self):
        os.environ["REOON_API_KEY"] = "fake"
        os.environ["PROSPEO_API_KEY"] = "fake"
        person = {"email": "Jane@X.com", "first_name": "Jane", "last_name": "Roe", "domain": "https://x.com"}
        with mock.patch.object(linkedin, "LOOKUP", [("prospeo", lambda url: person, "PROSPEO_API_KEY")]), \
             mock.patch.object(lp, "verify", return_value="valid"):
            r = self.client.post("/api/find", json={"linkedin_url": "https://linkedin.com/in/janeroe"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["email"], "jane@x.com")
        self.assertEqual(r.json["source"], "prospeo")
        hist = self.client.get("/api/history").json
        self.assertEqual(hist[0]["first_name"], "Jane")
        self.assertEqual(hist[0]["domain"], "x.com")

    def test_linkedin_requires_key(self):
        r = self.client.post("/api/find", json={"linkedin_url": "https://linkedin.com/in/x"})
        self.assertEqual(r.status_code, 400)

    def test_linkedin_not_found(self):
        os.environ["REOON_API_KEY"] = "fake"
        os.environ["APOLLO_API_KEY"] = "fake"
        with mock.patch.object(linkedin, "LOOKUP", [("apollo", lambda url: None, "APOLLO_API_KEY")]):
            r = self.client.post("/api/find", json={"linkedin_url": "https://linkedin.com/in/x"})
        self.assertEqual(r.json["status"], "not_found")


class OutreachTest(BaseTest):
    def test_push_to_instantly(self):
        self.set_fake_keys()
        os.environ["OUTREACH_API_KEY"] = "k"
        os.environ["OUTREACH_CAMPAIGN"] = "camp1"
        with mock.patch.object(lp, "find_email", return_value=("a@x.com", "valid", "hunter")):
            self.client.post("/api/find", json={"first_name": "A", "last_name": "B", "domain": "x.com"})
        with mock.patch.object(outreach.requests, "post") as post:
            post.return_value.ok = True
            post.return_value.status_code = 200
            r = self.client.post("/api/outreach/push", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["campaign_id"], "camp1")
        self.assertEqual(body["leads"][0]["email"], "a@x.com")

    def test_push_not_configured(self):
        r = self.client.post("/api/outreach/push", json={})
        self.assertEqual(r.status_code, 400)

    def test_smartlead_payload(self):
        os.environ["OUTREACH_PROVIDER"] = "smartlead"
        os.environ["OUTREACH_API_KEY"] = "k"
        os.environ["OUTREACH_CAMPAIGN"] = "42"
        with mock.patch.object(outreach.requests, "post") as post:
            post.return_value.ok = True
            ok, _ = outreach.push_leads([{"email": "a@x.com", "first_name": "A", "last_name": "B", "domain": "x.com"}])
        self.assertTrue(ok)
        self.assertIn("/campaigns/42/leads", post.call_args.args[0])


class ScheduleTest(BaseTest):
    def test_schedule_crud_and_run_with_sheet_writeback(self):
        self.set_fake_keys()
        r = self.client.post("/api/schedules", json={
            "name": "weekly", "csv_url": "https://example.com/leads.csv",
            "interval_hours": 24, "sheet_id": "sheet123"})
        self.assertEqual(r.status_code, 200)
        sid = r.json["id"]
        self.assertEqual(len(self.client.get("/api/schedules").json), 1)
        csv_text = "first_name,last_name,domain\nA,B,x.com\n"
        with mock.patch.object(webapp.requests, "get") as get, \
             mock.patch.object(lp, "find_email", return_value=("a@x.com", "valid", "hunter")), \
             mock.patch.object(sheets, "append_rows", return_value=(True, "ok")) as append:
            get.return_value.text = csv_text
            get.return_value.raise_for_status = lambda: None
            r = self.client.post(f"/api/schedules/{sid}/run", json={})
            self.assertEqual(r.status_code, 200)
            job = wait_job(self.client, r.json["job_id"])
            self.assertEqual(job["status"], "done")
            time.sleep(0.3)  # on_done callback
            append.assert_called_once()
            self.assertEqual(append.call_args.args[0], "sheet123")
            values = append.call_args.args[1]
            self.assertEqual(values[1][3], "a@x.com")
        self.client.post(f"/api/schedules/{sid}", json={"enabled": False})
        self.assertEqual(self.client.get("/api/schedules").json[0]["enabled"], 0)
        self.client.delete(f"/api/schedules/{sid}")
        self.assertEqual(len(self.client.get("/api/schedules").json), 0)

    def test_schedule_bad_url(self):
        r = self.client.post("/api/schedules", json={"name": "x", "csv_url": "ftp://bad"})
        self.assertEqual(r.status_code, 400)

    def test_scheduler_tick_respects_interval(self):
        self.set_fake_keys()
        r = self.client.post("/api/schedules", json={
            "name": "s", "csv_url": "https://example.com/l.csv", "interval_hours": 24})
        sid = r.json["id"]
        with mock.patch.object(webapp, "run_schedule") as run:
            webapp.scheduler_tick()  # last_run null -> due
            run.assert_called_once_with(sid)
            run.reset_mock()
            conn = webapp.db()
            try:
                from datetime import datetime
                conn.execute("UPDATE schedules SET last_run=? WHERE id=?",
                             (datetime.now().isoformat(timespec="seconds"), sid))
                conn.commit()
            finally:
                conn.close()
            webapp.scheduler_tick()  # abhi chala tha -> due nahi
            run.assert_not_called()


class InfraTest(BaseTest):
    def test_backup_creates_and_prunes(self):
        conn = lp.db()
        conn.close()
        os.environ["BACKUP_KEEP"] = "2"
        os.environ["DATA_DIR"] = tempfile.mkdtemp()
        paths = [backup.run_backup() for _ in range(3)]
        self.assertTrue(os.path.exists(paths[-1]))
        backup_dir = os.path.dirname(paths[-1])
        remaining = [f for f in os.listdir(backup_dir) if f.endswith(".db")]
        self.assertEqual(len(remaining), 2)  # retention prune
        os.environ.pop("BACKUP_KEEP", None)
        os.environ.pop("DATA_DIR", None)

    def test_throttle_enforces_interval(self):
        os.environ["PROVIDER_MIN_INTERVAL"] = "0.15"
        calls = []
        wrapped = throttle._throttled("testprov", lambda: calls.append(time.monotonic()))
        wrapped()
        wrapped()
        self.assertGreaterEqual(calls[1] - calls[0], 0.14)
        os.environ.pop("PROVIDER_MIN_INTERVAL", None)

    def test_parallel_job_completes(self):
        self.set_fake_keys()
        os.environ["PARALLEL_LEADS"] = "3"
        rows = "".join(f"P{i},L{i},d{i}.com\n" for i in range(9)).encode()
        csv_data = b"first_name,last_name,domain\n" + rows
        with mock.patch.object(lp, "find_email", side_effect=lambda conn, f, l, d: (f"{f.lower()}@{d}", "valid", "hunter")):
            r = self.client.post("/api/jobs", data={"file": (io.BytesIO(csv_data), "many.csv")})
            job = wait_job(self.client, r.json["job_id"], timeout=20)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["done"], 9)
        self.assertTrue(all(x["status"] == "valid" for x in job["rows"]))
        os.environ.pop("PARALLEL_LEADS", None)

    def test_sheets_append_with_service_account(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps({
            "client_email": "bot@project.iam.gserviceaccount.com", "private_key": pem})
        with mock.patch.object(sheets.requests, "post") as post:
            post.return_value.ok = True
            post.return_value.json.return_value = {"access_token": "tok"}
            post.return_value.raise_for_status = lambda: None
            ok, info = sheets.append_rows("sheet1", [["h1"], ["v1"]])
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)  # token exchange + append
        self.assertIn("sheet1", post.call_args.args[0])
        os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)


class WaterfallTest(unittest.TestCase):
    """Pipeline logic with fake provider functions (PROVIDERS patched)."""

    def setUp(self):
        reset_db()
        for env in webapp.KEY_ENVS:
            os.environ.pop(env, None)
        os.environ["REOON_API_KEY"] = "fake"
        os.environ["HUNTER_API_KEY"] = "fake"
        os.environ["PROSPEO_API_KEY"] = "fake"
        os.environ["HUNTER_LIMIT"] = "5"
        os.environ["PROSPEO_LIMIT"] = "5"
        self.conn = lp.db()

    def tearDown(self):
        self.conn.close()

    def test_waterfall_fallback_to_second_provider(self):
        providers = [
            ("hunter", lambda f, l, d: "bad@x.com", "HUNTER_API_KEY", 5),
            ("prospeo", lambda f, l, d: "good@x.com", "PROSPEO_API_KEY", 5),
        ]
        with mock.patch.object(lp, "PROVIDERS", providers), \
             mock.patch.object(lp, "verify", side_effect=["invalid", "valid"]):
            email, status, source = lp.find_email(self.conn, "A", "B", "x.com")
        self.assertEqual((email, status, source), ("good@x.com", "valid", "prospeo"))
        used = dict(self.conn.execute("SELECT provider, used FROM quotas"))
        self.assertEqual(used["hunter"], 1)
        self.assertEqual(used["prospeo"], 1)

    def test_waterfall_risky_fallback_and_quota_skip(self):
        os.environ["HUNTER_LIMIT"] = "0"  # exhausted -> skipped
        self.conn = lp.db()  # re-sync limits from env
        providers = [
            ("hunter", lambda f, l, d: "skip@x.com", "HUNTER_API_KEY", 5),
            ("prospeo", lambda f, l, d: "catchall@x.com", "PROSPEO_API_KEY", 5),
        ]
        with mock.patch.object(lp, "PROVIDERS", providers), \
             mock.patch.object(lp, "verify", return_value="catch_all"):
            email, status, source = lp.find_email(self.conn, "A", "B", "x.com")
        self.assertEqual((email, status, source), ("catchall@x.com", "risky", "prospeo"))

    def test_waterfall_not_found(self):
        providers = [("hunter", lambda f, l, d: None, "HUNTER_API_KEY", 5)]
        with mock.patch.object(lp, "PROVIDERS", providers):
            email, status, source = lp.find_email(self.conn, "A", "B", "x.com")
        self.assertEqual((email, status, source), (None, "not_found", None))


if __name__ == "__main__":
    unittest.main()