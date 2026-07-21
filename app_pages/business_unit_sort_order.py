# Business Unit Sort Order admin page with event-sourced audit trail
# Co-authored with CoCo
import streamlit as st
import pandas as pd
import json
import uuid

session = st.session_state["conn"].session()

@st.cache_data(ttl=30)
def load_master_data():
    df = session.sql("""
        SELECT
            "(PK) ID"           AS "ID",
            "Business Unit",
            "Short Name",
            "Group",
            "Sort Order",
            "Enabled",
            "Changed By",
            "Changed On"
        FROM CONFIG.SILVER."Business_Unit_Sort_Order_MasterData" WHERE "Enabled" = true
        ORDER BY "Sort Order"
    """).to_pandas()
    df["Enabled"] = df["Enabled"].astype(bool)
    return df

def write_events(events: list[dict]):
    if not events:
        return
    user = st.user.user_name
    for evt in events:
        evt["Changed By"] = user
        payload = json.dumps(evt)
        session.sql("""
            INSERT INTO CONFIG.RAW.BUSINESS_UNIT_SORT_ORDER_EVENT_DATA
                (SERIALIZED_SOURCE, EVENT_TIME)
            SELECT
                PARSE_JSON(?),
                CURRENT_TIMESTAMP()
        """, params=[payload]).collect()

with st.spinner("Loading data..."):
    master_df = load_master_data()

tab_edit, tab_add, tab_bulk = st.tabs(["Edit Existing", "Add New", "Bulk Upload"])

with tab_edit:
    st.caption("Edit rows below. Changes are saved as change events with full audit trail.")

    group_filter = st.selectbox(
        "Filter by Group",
        options=["All"] + sorted(master_df["Group"].unique().tolist()),
        key="buso_group_filter",
    )

    if group_filter == "All":
        filtered_df = master_df
    else:
        filtered_df = master_df[master_df["Group"] == group_filter].reset_index(drop=True)

    display_df = filtered_df.copy()
    display_df.insert(0, "Delete", False)

    edited_df = st.data_editor(
        display_df,
        key=f"buso_editor_{group_filter}",
        disabled=["ID", "Changed By", "Changed On", "Enabled"],
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "ID": None,
            "Enabled": None,
        },
    )

    if st.button("Save Changes", key="buso_save_edits", type="primary"):
        changes = []
        deletes = []
        for idx in range(len(filtered_df)):
            orig = filtered_df.iloc[idx]
            curr = edited_df.iloc[idx]

            if curr["Delete"]:
                deletes.append(orig["ID"])
                changes.append({
                    "ID": orig["ID"],
                    "Business Unit": orig["Business Unit"],
                    "Short Name": orig["Short Name"],
                    "Group": orig["Group"],
                    "Sort Order": int(orig["Sort Order"]),
                    "Enabled": False,
                })
                continue

            curr_comparable = curr.drop("Delete")
            if not orig.equals(curr_comparable):
                changes.append({
                    "ID": orig["ID"],
                    "Business Unit": curr["Business Unit"],
                    "Short Name": curr["Short Name"],
                    "Group": curr["Group"],
                    "Sort Order": int(curr["Sort Order"]),
                    "Enabled": bool(curr["Enabled"]),
                })

        if changes:
            if deletes:
                st.warning(f"Disabling {len(deletes)} record(s).")
            write_events(changes)
            st.cache_data.clear()
            st.rerun()
        else:
            st.info("No changes detected.")

with tab_add:
    st.subheader("Add New Record")
    col1, col2 = st.columns(2)
    with col1:
        new_bu = st.text_input("Business Unit", key="buso_new_bu")
    with col2:
        new_short = st.text_input("Short Name", key="buso_new_short")
    col3, col4 = st.columns(2)
    with col3:
        new_group = st.text_input("Group", key="buso_new_group")
    with col4:
        new_sort = st.number_input("Sort Order", value=0, step=1, key="buso_new_sort")

    if st.button("Add Record", type="primary", key="buso_add"):
        if not all([new_bu, new_short, new_group]):
            st.error("Business Unit, Short Name, and Group are required.")
        else:
            new_id = str(uuid.uuid4())
            evt = {
                "ID": new_id,
                "Business Unit": new_bu,
                "Short Name": new_short,
                "Group": new_group,
                "Sort Order": new_sort,
                "Enabled": True,
            }
            write_events([evt])
            st.cache_data.clear()
            st.success(f"Added record: {new_id}. Refresh to see updates.")

with tab_bulk:
    st.subheader("Bulk Upload")
    st.caption("Upload a CSV file with columns: Business Unit, Short Name, Group, Sort Order")

    uploaded_file = st.file_uploader("Choose a CSV file", type="csv", key="bulk_upload")

    if uploaded_file is not None:
        try:
            upload_df = pd.read_csv(uploaded_file, dtype=str).fillna("")
            required_cols = {"Business Unit", "Short Name", "Group", "Sort Order"}
            missing_cols = required_cols - set(upload_df.columns)
            if missing_cols:
                st.error(f"CSV is missing required columns: {', '.join(missing_cols)}")
            else:
                extra_cols = set(upload_df.columns) - required_cols
                if extra_cols:
                    st.warning(f"Ignoring unrecognized columns: {', '.join(extra_cols)}")
                upload_df = upload_df[[c for c in required_cols if c in upload_df.columns]]

                invalid_rows = upload_df[upload_df["Business Unit"].str.strip() == ""]
                if not invalid_rows.empty:
                    st.error(f"{len(invalid_rows)} row(s) are missing a Business Unit value.")
                else:
                    existing_mask = upload_df["Business Unit"].isin(master_df["Business Unit"])
                    num_updates = existing_mask.sum()
                    num_new = len(upload_df) - num_updates
                    if num_updates > 0:
                        st.info(f"{num_updates} row(s) match existing Business Units and will be treated as updates.")
                    if num_new > 0:
                        st.info(f"{num_new} row(s) will be added as new records.")

                    st.dataframe(upload_df, hide_index=True, use_container_width=True)

                    if st.button("Confirm Bulk Upload", type="primary", key="bulk_confirm"):
                        events = []
                        bu_to_id = dict(zip(master_df["Business Unit"], master_df["ID"]))
                        for _, row in upload_df.iterrows():
                            bu = row["Business Unit"].strip()
                            existing_id = bu_to_id.get(bu)
                            events.append({
                                "ID": existing_id if existing_id else str(uuid.uuid4()),
                                "Business Unit": bu,
                                "Short Name": row["Short Name"].strip(),
                                "Group": row["Group"].strip(),
                                "Sort Order": int(row["Sort Order"]) if row["Sort Order"].strip() else 0,
                                "Enabled": True,
                            })
                        total = len(events)
                        progress_bar = st.progress(0, text=f"Processing 0 of {total}...")
                        user = st.user.user_name
                        for i, evt in enumerate(events, 1):
                            evt["Changed By"] = user
                            payload = json.dumps(evt)
                            session.sql("""
                                INSERT INTO CONFIG.RAW.BUSINESS_UNIT_SORT_ORDER_EVENT_DATA
                                    (SERIALIZED_SOURCE, EVENT_TIME)
                                SELECT
                                    PARSE_JSON(?),
                                    CURRENT_TIMESTAMP()
                            """, params=[payload]).collect()
                            progress_bar.progress(i / total, text=f"Processed {i} of {total}...")
                        st.cache_data.clear()
                        st.rerun()
        except Exception as e:
            st.error(f"Error reading CSV: {e}")
