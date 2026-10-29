"""
Lead Finder: simple screen for the email waterfall.
Run:  streamlit run app.py
(lead_pipeline.py must be in the same folder)
"""
import contextlib
import io
import os
from datetime import date

import pandas as pd
import streamlit as st

import lead_pipeline as lp

st.set_page_config(page_title="Lead Finder", page_icon="📧", layout="wide")
st.title("📧 Lead Finder")
st.caption("Naam + company domain daalo → email dhundo → verify karo")

STATUS_LABEL = {"valid": "✅ Sahi email", "risky": "⚠️ Risky (check karo)", "not_found": "❌ Nahi mila"}

# ------------------------------------------------------------------ sidebar: keys
with st.sidebar:
    st.header("🔑 API Keys")
    st.caption("Keys sirf tumhare computer par rehti hain. Jo service use nahi kar rahe, khali chhod do.")
    for label, env in [
        ("Reoon (verify, zaruri)", "REOON_API_KEY"),
        ("Hunter", "HUNTER_API_KEY"),
        ("Prospeo", "PROSPEO_API_KEY"),
        ("Apollo", "APOLLO_API_KEY"),
    ]:
        val = st.text_input(label, value=os.getenv(env, ""), type="password", key=env)
        if val.strip():
            os.environ[env] = val.strip()
        else:
            os.environ.pop(env, None)

    st.divider()
    st.header("📊 Monthly limits")
    st.caption("Apne dashboard se asli credits dekh kar daalo.")
    for p_name, _, _, default in lp.PROVIDERS:
        n = st.number_input(f"{p_name.capitalize()} limit", min_value=0, value=int(os.getenv(f"{p_name.upper()}_LIMIT", default)), key=f"lim_{p_name}")
        os.environ[f"{p_name.upper()}_LIMIT"] = str(int(n))


# ------------------------------------------------------------------ helpers
def clean_domain(d):
    d = str(d).strip().lower()
    for prefix in ("https://", "http://", "www."):
        d = d.removeprefix(prefix)
    return d.strip("/")


def process(conn, first, last, domain):
    """Returns (email, status, source, log_text). Skips leads that were already found."""
    cached = conn.execute(
        "SELECT email, status, source FROM leads WHERE first_name=? AND last_name=? AND domain=? AND status='valid'",
        (first, last, domain),
    ).fetchone()
    if cached:
        return cached["email"], cached["status"], cached["source"], "Pehle se mil chuka tha, credit kharch nahi hua."
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        email, status, source = lp.find_email(conn, first, last, domain)
    conn.execute(
        "INSERT OR REPLACE INTO leads VALUES (?,?,?,?,?,?,?)",
        (first, last, domain, email, status, source, date.today().isoformat()),
    )
    conn.commit()
    return email, status, source, log.getvalue()


def ready():
    if not os.getenv("REOON_API_KEY"):
        st.warning("Pehle left sidebar mein Reoon key daalo.")
        return False
    if not any(os.getenv(env) for _, _, env, _ in lp.PROVIDERS):
        st.warning("Kam se kam ek finder (Hunter / Prospeo / Apollo) ki key daalo.")
        return False
    return True


tab1, tab2, tab3 = st.tabs(["1️⃣ Ek lead", "2️⃣ CSV se bahut saare", "3️⃣ Quota aur history"])

# ------------------------------------------------------------------ tab 1: single lead
with tab1:
    c1, c2, c3 = st.columns(3)
    first = c1.text_input("First name", placeholder="Patrick")
    last = c2.text_input("Last name", placeholder="Collison")
    domain = c3.text_input("Company domain", placeholder="stripe.com")
    if st.button("🔍 Email dhundo", type="primary"):
        if not (first.strip() and last.strip() and domain.strip()):
            st.warning("Teeno boxes bharo.")
        elif ready():
            conn = lp.db()
            with st.spinner("Dhundh raha hoon aur verify kar raha hoon..."):
                email, status, source, log = process(conn, first.strip(), last.strip(), clean_domain(domain))
            if email:
                st.success(f"{STATUS_LABEL[status]}: **{email}** (source: {source})")
            else:
                st.error(STATUS_LABEL["not_found"])
            with st.expander("Kya hua? (details)"):
                st.code(log or "-")

# ------------------------------------------------------------------ tab 2: CSV
with tab2:
    st.write("CSV mein ye 3 columns chahiye: `first_name`, `last_name`, `domain`")
    sample = pd.DataFrame({"first_name": ["Patrick"], "last_name": ["Collison"], "domain": ["stripe.com"]})
    st.download_button("⬇️ Sample CSV download karo", sample.to_csv(index=False), "sample_leads.csv", "text/csv")

    up = st.file_uploader("Apni CSV yahan daalo", type="csv")
    if up is not None:
        df = pd.read_csv(up)
        df.columns = [c.strip().lower() for c in df.columns]
        missing = {"first_name", "last_name", "domain"} - set(df.columns)
        if missing:
            st.error(f"Ye columns missing hain: {', '.join(sorted(missing))}")
        else:
            st.write(f"{len(df)} leads mile. Preview:")
            st.dataframe(df.head(), width="stretch")
            if st.button("▶️ Start", type="primary") and ready():
                conn = lp.db()
                bar = st.progress(0.0)
                rows = []
                for i, r in df.iterrows():
                    f_, l_, d_ = str(r["first_name"]).strip(), str(r["last_name"]).strip(), clean_domain(r["domain"])
                    bar.progress((i + 1) / len(df), text=f"{i + 1}/{len(df)}: {f_} {l_} @ {d_}")
                    email, status, source, _ = process(conn, f_, l_, d_)
                    rows.append({**r.to_dict(), "email": email or "", "status": status, "source": source or ""})
                st.session_state["csv_result"] = pd.DataFrame(rows)
                bar.empty()

    if "csv_result" in st.session_state:
        res = st.session_state["csv_result"]
        st.success(
            f"Ho gaya: {(res.status == 'valid').sum()} sahi, "
            f"{(res.status == 'risky').sum()} risky, {(res.status == 'not_found').sum()} nahi mile"
        )
        st.dataframe(res, width="stretch")
        st.download_button("⬇️ Results download karo", res.to_csv(index=False), "results.csv", "text/csv", type="primary")

# ------------------------------------------------------------------ tab 3: quota + history
with tab3:
    conn = lp.db()
    st.subheader("Credits ka hisaab")
    q = pd.read_sql_query("SELECT provider, used, monthly_limit, resets_on FROM quotas", conn)
    q["bacha"] = q["monthly_limit"] - q["used"]
    st.dataframe(q, width="stretch", hide_index=True)
    st.caption("Ye hisaab app ka apna estimate hai. Kabhi kabhi asli dashboard se milaa lo.")

    st.subheader("Ab tak ke leads")
    hist = pd.read_sql_query("SELECT * FROM leads ORDER BY found_at DESC", conn)
    st.dataframe(hist, width="stretch", hide_index=True)
