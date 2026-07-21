# Balance Sheet Rules admin page with event-sourced audit trail
# Co-authored with CoCo
import streamlit as st
import pandas as pd
import json
import uuid

session = st.session_state["conn"].session()

# st.set_page_config(page_title="Balance Sheet Rules Admin")
# st.title("Balance Sheet Rules Admin")

@st.cache_data(ttl=600)
def load_master_data():
    df = session.sql("""
        SELECT
            "(PK) ID"           AS "ID",
            "Code",
            "Description",
            "Enabled",
            "Changed By",
            "Changed On"
        FROM CONFIG.SILVER."Balance_Sheet_Rules_MasterData" WHERE "Enabled" = true
        ORDER BY "Code"
    """).to_pandas()
    df["Enabled"] = df["Enabled"].astype(bool)
    return df

def write_events(events: list):
    if not events:
        return
    user = st.user.user_name
    for evt in events:
        evt["Changed By"] = user
        payload = json.dumps(evt)
        session.sql("""
            INSERT INTO CONFIG.RAW.BALANCE_SHEET_RULES_EVENT_DATA
                (SERIALIZED_SOURCE)
            SELECT
                PARSE_JSON(?)
        """, params=[payload]).collect()

def find_duplicate_code(df, code, exclude_id=None):
    dupes = df[(df["Code"] == code) & (df["Enabled"] == True)]
    if exclude_id:
        dupes = dupes[dupes["ID"] != exclude_id]
    return dupes

with st.spinner("Loading data..."):
    master_df = load_master_data()

# tab_edit, tab_add, tab_audit = st.tabs(["Edit Rules", "Add Rule", "Audit Trail"])
tab_edit, tab_add, tab_bulk = st.tabs(["Edit Rules", "Add Rule", "Bulk Upload"])

with tab_edit:
    st.caption("Edit rows below. Changes are saved as change events with full audit trail.")

    code_filter = st.selectbox(
        "Filter by Code",
        options=["All"] + sorted(master_df["Code"].unique().tolist()),
        key="code_filter"
    )

    if code_filter == "All":
        filtered_df = master_df
    else:
        filtered_df = master_df[master_df["Code"] == code_filter].reset_index(drop=True)

    display_df = filtered_df.copy()
    display_df.insert(0, "Delete", False)

    edited_df = st.data_editor(
        display_df,
        key=f"editor_{code_filter}",
        disabled=["ID", "Changed By", "Changed On"],
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "ID": None,
            "Enabled": None
        }
    )

    if st.button("Save Changes", key="save_edit"):
        changes = []
        deletes = []
        dupe_warnings = []
        for idx in range(len(filtered_df)):
            orig = filtered_df.iloc[idx]
            curr = edited_df.iloc[idx]

            if curr["Delete"]:
                deletes.append(orig["ID"])
                changes.append({
                    "ID": orig["ID"],
                    "Code": orig["Code"],
                    "Description": orig["Description"],
                    "Enabled": False
                })
                continue

            curr_comparable = curr.drop("Delete")
            if not orig.equals(curr_comparable):
                code_changed = orig["Code"] != curr["Code"]
                if code_changed and bool(curr["Enabled"]):
                    dupes = find_duplicate_code(
                        master_df[master_df["ID"] != orig["ID"]],
                        curr["Code"]
                    )
                    if not dupes.empty:
                        dupe_warnings.append((orig["ID"], curr["Code"], dupes))

                changes.append({
                    "ID": orig["ID"],
                    "Code": curr["Code"],
                    "Description": curr["Description"],
                    "Enabled": bool(curr["Enabled"])
                })

        if dupe_warnings:
            st.error(f"Found duplicate Code(s) in {len(dupe_warnings)} edit(s). Changes not saved.")
            for row_id, code, dupes in dupe_warnings:
                st.warning(f"Row `{row_id}`: Code `{code}` already exists.")
                st.dataframe(dupes[["ID", "Code", "Description"]], hide_index=True, use_container_width=True)
        elif changes:
            if deletes:
                st.warning(f"Disabling {len(deletes)} rule(s).")
            write_events(changes)
            st.cache_data.clear()
            st.rerun()
        else:
            st.info("No changes detected.")

with tab_add:
    st.subheader("Add New Rule")
    col1, col2 = st.columns(2)
    with col1:
        new_code = st.text_input("Code")
    with col2:
        new_desc = st.text_input("Description")

    if st.button("Add Rule", key="add_rule"):
        if not all([new_code, new_desc]):
            st.error("All fields are required.")
        else:
            dupes = find_duplicate_code(master_df, new_code)
            if not dupes.empty:
                st.error(f"Code `{new_code}` already exists:")
                st.dataframe(dupes[["ID", "Code", "Description"]], hide_index=True, use_container_width=True)
            else:
                new_id = str(uuid.uuid4())
                evt = {
                    "ID": new_id,
                    "Code": new_code,
                    "Description": new_desc,
                    "Enabled": True
                }
                write_events([evt])
                st.cache_data.clear()
                st.success(f"Added rule: {new_id}. Refresh to see updates.")

with tab_bulk:
    st.subheader("Bulk Upload")
    st.caption("Upload a CSV file with columns: Code, Description")

    uploaded_file = st.file_uploader("Choose a CSV file", type="csv", key="bulk_upload")

    if uploaded_file is not None:
        try:
            upload_df = pd.read_csv(uploaded_file, dtype=str).fillna("")
            required_cols = {"Code", "Description"}
            missing_cols = required_cols - set(upload_df.columns)
            if missing_cols:
                st.error(f"CSV is missing required columns: {', '.join(missing_cols)}")
            else:
                extra_cols = set(upload_df.columns) - required_cols
                if extra_cols:
                    st.warning(f"Ignoring unrecognized columns: {', '.join(extra_cols)}")
                upload_df = upload_df[[c for c in required_cols if c in upload_df.columns]]

                invalid_rows = upload_df[upload_df["Code"].str.strip() == ""]
                if not invalid_rows.empty:
                    st.error(f"{len(invalid_rows)} row(s) are missing a Code value.")
                else:
                    existing_mask = upload_df["Code"].isin(master_df["Code"])
                    num_updates = existing_mask.sum()
                    num_new = len(upload_df) - num_updates
                    if num_updates > 0:
                        st.info(f"{num_updates} row(s) match existing Codes and will be treated as updates.")
                    if num_new > 0:
                        st.info(f"{num_new} row(s) will be added as new rules.")

                    st.dataframe(upload_df, hide_index=True, use_container_width=True)

                    if st.button("Confirm Bulk Upload", type="primary", key="bulk_confirm"):
                        events = []
                        code_to_id = dict(zip(master_df["Code"], master_df["ID"]))
                        for _, row in upload_df.iterrows():
                            code = row["Code"].strip()
                            existing_id = code_to_id.get(code)
                            events.append({
                                "ID": existing_id if existing_id else str(uuid.uuid4()),
                                "Code": code,
                                "Description": row["Description"].strip(),
                                "Enabled": True,
                            })
                        total = len(events)
                        progress_bar = st.progress(0, text=f"Processing 0 of {total}...")
                        user = st.user.user_name
                        for i, evt in enumerate(events, 1):
                            evt["Changed By"] = user
                            payload = json.dumps(evt)
                            session.sql("""
                                INSERT INTO CONFIG.RAW.BALANCE_SHEET_RULES_EVENT_DATA
                                    (SERIALIZED_SOURCE)
                                SELECT
                                    PARSE_JSON(?)
                            """, params=[payload]).collect()
                            progress_bar.progress(i / total, text=f"Processed {i} of {total}...")
                        st.cache_data.clear()
                        st.rerun()
        except Exception as e:
            st.error(f"Error reading CSV: {e}")

# with tab_audit:
#     st.subheader("Audit Trail")

#     all_audit_ids = session.sql("""
#         SELECT DISTINCT "ID"
#         FROM CONFIG.BRONZE."Balance_Sheet_Rules_AuditData"
#         ORDER BY "ID"
#     """).to_pandas()["ID"].tolist()

#     filter_id = st.selectbox(
#         "Filter by ID (optional)",
#         options=["All"] + all_audit_ids
#     )

#     where = ""
#     if filter_id != "All":
#         where = f'WHERE "ID" = \'{filter_id}\''
#     audit_df = session.sql(f"""
#         SELECT
#             "ID",
#             "Code",
#             "Description",
#             "Enabled",
#             "Changed By",
#             "Changed On",
#             "Change Offset"
#         FROM CONFIG.BRONZE."Balance_Sheet_Rules_AuditData"
#         {where}
#         ORDER BY "ID", "Change Offset"
#     """).to_pandas()
#     st.dataframe(audit_df, hide_index=True, use_container_width=True)

#     if st.button("Export CSV"):
#         csv = audit_df.to_csv(index=False)
#         st.code(csv, language="csv")

