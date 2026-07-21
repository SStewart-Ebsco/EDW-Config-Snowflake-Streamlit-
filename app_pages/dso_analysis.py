# DSO Analysis admin page with event-sourced audit trail
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
            "Date",
            "Measure",
            "AR",
            "TTMSales",
            "DSO",
            "Enabled",
            "Changed By",
            "Changed On"
        FROM CONFIG.SILVER."DSO_Analysis_MasterData" WHERE "Enabled" = true
        ORDER BY "Date" DESC, "Measure"
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
            INSERT INTO CONFIG.RAW.DSO_ANALYSIS_EVENT_DATA
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

    measure_filter = st.selectbox(
        "Filter by Measure",
        options=["All"] + sorted(master_df["Measure"].unique().tolist()),
        key="dso_measure_filter",
    )

    if measure_filter == "All":
        filtered_df = master_df
    else:
        filtered_df = master_df[master_df["Measure"] == measure_filter].reset_index(drop=True)

    display_df = filtered_df.copy()
    display_df.insert(0, "Delete", False)

    edited_df = st.data_editor(
        display_df,
        key=f"dso_editor_{measure_filter}",
        disabled=["ID", "Changed By", "Changed On", "Enabled"],
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "ID": None,
            "Enabled": None,
        },
    )

    if st.button("Save Changes", key="dso_save_edits", type="primary"):
        changes = []
        deletes = []
        for idx in range(len(filtered_df)):
            orig = filtered_df.iloc[idx]
            curr = edited_df.iloc[idx]

            if curr["Delete"]:
                deletes.append(orig["ID"])
                changes.append({
                    "ID": orig["ID"],
                    "Date": str(orig["Date"]),
                    "Measure": orig["Measure"],
                    "AR": str(orig["AR"]),
                    "TTMSales": str(orig["TTMSales"]),
                    "DSO": str(orig["DSO"]),
                    "Enabled": False,
                })
                continue

            curr_comparable = curr.drop("Delete")
            if not orig.equals(curr_comparable):
                changes.append({
                    "ID": orig["ID"],
                    "Date": str(curr["Date"]),
                    "Measure": curr["Measure"],
                    "AR": str(curr["AR"]),
                    "TTMSales": str(curr["TTMSales"]),
                    "DSO": str(curr["DSO"]),
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
    col1, col2, col3 = st.columns(3)
    with col1:
        new_date = st.date_input("Date", key="dso_new_date")
    with col2:
        new_measure = st.text_input("Measure", key="dso_new_measure")
    with col3:
        new_ar = st.number_input("AR", value=0.00, format="%.2f", key="dso_new_ar")
    col4, col5 = st.columns(2)
    with col4:
        new_ttmsales = st.number_input("TTM Sales", value=0.00, format="%.2f", key="dso_new_ttmsales")
    with col5:
        new_dso = st.number_input("DSO", value=0.00, format="%.2f", key="dso_new_dso")

    if st.button("Add Record", type="primary", key="dso_add"):
        if not all([new_date, new_measure]):
            st.error("Date and Measure are required.")
        else:
            new_id = str(uuid.uuid4())
            evt = {
                "ID": new_id,
                "Date": str(new_date),
                "Measure": new_measure,
                "AR": str(new_ar),
                "TTMSales": str(new_ttmsales),
                "DSO": str(new_dso),
                "Enabled": True,
            }
            write_events([evt])
            st.cache_data.clear()
            st.rerun()

with tab_bulk:
    st.subheader("Bulk Upload")
    st.caption("Upload a CSV with columns: Date, Measure, AR, TTMSales, DSO")

    uploaded_file = st.file_uploader("Choose a CSV file", type=["csv"], key="dso_csv_upload")

    if uploaded_file is not None:
        try:
            upload_df = pd.read_csv(uploaded_file, dtype=str).fillna("")
            required_cols = {"Date", "Measure", "AR", "TTMSales", "DSO"}
            missing_cols = required_cols - set(upload_df.columns)

            if missing_cols:
                st.error(f"Missing required columns: {', '.join(missing_cols)}")
            else:
                master_key = master_df["Date"].astype(str) + "|" + master_df["Measure"].astype(str)
                upload_key = upload_df["Date"].str.strip() + "|" + upload_df["Measure"].str.strip()
                existing_mask = upload_key.isin(master_key)
                num_updates = existing_mask.sum()
                num_new = len(upload_df) - num_updates
                if num_updates > 0:
                    st.info(f"{num_updates} row(s) match existing Date+Measure and will be treated as updates.")
                if num_new > 0:
                    st.info(f"{num_new} row(s) will be added as new records.")

                st.dataframe(upload_df, use_container_width=True, hide_index=True)

                if st.button("Confirm Bulk Upload", type="primary", key="dso_bulk_import"):
                    events = []
                    key_to_id = dict(zip(master_key, master_df["ID"]))
                    for _, row in upload_df.iterrows():
                        k = row["Date"].strip() + "|" + row["Measure"].strip()
                        existing_id = key_to_id.get(k)
                        events.append({
                            "ID": existing_id if existing_id else str(uuid.uuid4()),
                            "Date": str(row["Date"]).strip(),
                            "Measure": str(row["Measure"]).strip(),
                            "AR": str(row["AR"]).strip(),
                            "TTMSales": str(row["TTMSales"]).strip(),
                            "DSO": str(row["DSO"]).strip(),
                            "Enabled": True,
                        })
                    total = len(events)
                    progress_bar = st.progress(0, text=f"Processing 0 of {total}...")
                    user = st.user.user_name
                    for i, evt in enumerate(events, 1):
                        evt["Changed By"] = user
                        payload = json.dumps(evt)
                        session.sql("""
                            INSERT INTO CONFIG.RAW.DSO_ANALYSIS_EVENT_DATA
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
