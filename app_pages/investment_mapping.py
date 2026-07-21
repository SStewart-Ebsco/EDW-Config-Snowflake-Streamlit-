# Investment Mapping admin page with event-sourced audit trail
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
            "Investment",
            "Profit Center",
            "Is Exclusion",
            "GL Min",
            "GL Max",
            "Enabled",
            "Changed By",
            "Changed On"
        FROM CONFIG.SILVER."Investment_Mapping_GL_MasterData" WHERE "Enabled" = true 
        ORDER BY "Investment", "Profit Center"
    """).to_pandas()
    df["Enabled"] = df["Enabled"].astype(bool)
    return df

def write_events(events: list[dict]):
    if not events:
        return
    
    # SMS: In a container, setup, this gets the owner (svc. user) not the viewer
    # user = session.sql("SELECT CURRENT_USER()").collect()[0][0]
    
    # This will get the viewer's user identity
    user = st.user.user_name
    
    for evt in events:
        evt["Changed By"] = user
        payload = json.dumps(evt)
        session.sql("""
            INSERT INTO CONFIG.RAW.INVESTMENT_MAPPING_GL_EVENT_DATA
                (SERIALIZED_SOURCE, EVENT_TIME)
            SELECT
                PARSE_JSON(?),
                CURRENT_TIMESTAMP()
        """, params=[payload]).collect()

def find_overlaps(df, investment, profit_center, gl_min, gl_max):
    filtered_df = df[
        (df["Investment"] == investment)
        & (df["Profit Center"] == profit_center)
        & (df["Enabled"] == True)
    ]
    overlaps = filtered_df[
        (filtered_df["GL Min"] <= gl_max)
        & (filtered_df["GL Max"] >= gl_min)
    ]
    return overlaps

with st.spinner("Loading data..."):
    master_df = load_master_data()
    
tab_edit, tab_add, tab_bulk = st.tabs(["Edit Existing", "Add New", "Bulk Upload"])

with tab_edit:
    st.caption("Edit rows below. Changes are saved as change events with full audit trail.")

    investment_filter = st.selectbox(
        "Filter by Investment",
        options=["All"] + sorted(master_df["Investment"].dropna().unique().tolist()),
        key="investment_filter",
    )

    if investment_filter == "All":
        filtered_df = master_df
    else:
        filtered_df = master_df[master_df["Investment"] == investment_filter].reset_index(drop=True)

    display_df = filtered_df.copy()
    display_df.insert(0, "Delete", False)

    edited_df = st.data_editor(
        display_df,
        key=f"editor_{investment_filter}",
        disabled=["ID", "Changed By", "Changed On", "Enabled"],
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "ID": None,
            "Enabled": None,
        },        
    )

    if st.button("Save Changes", key="save_edits", type="primary"):
        changes = []
        overlap_warnings = []
        deletes = []
        for idx in range(len(filtered_df)):
            orig = filtered_df.iloc[idx]
            curr = edited_df.iloc[idx]

            if curr["Delete"]:
                deletes.append(orig["ID"])
                changes.append({
                    "ID": orig["ID"],
                    "Investment": orig["Investment"],
                    "Profit Center": orig["Profit Center"],
                    "Is Exclusion": bool(orig["Is Exclusion"]) if orig["Is Exclusion"] is not None else False,
                    "GL Min": orig["GL Min"],
                    "GL Max": orig["GL Max"],
                    "Enabled": False,
                })
                continue

            curr_comparable = curr.drop("Delete")
            if not orig.equals(curr_comparable):
                effective_max = curr["GL Max"] if curr["GL Max"] else curr["GL Min"]
                if effective_max < curr["GL Min"]:
                    st.error(f"Row `{orig['ID']}`: GL Max cannot be less than GL Min.")
                    changes = []
                    break

                gl_changed = (
                    orig["Investment"] != curr["Investment"]
                    or orig["Profit Center"] != curr["Profit Center"]
                    or orig["GL Min"] != curr["GL Min"]
                    or orig["GL Max"] != curr["GL Max"]
                )
                if gl_changed and bool(curr["Enabled"]):
                    overlaps = find_overlaps(
                        master_df[master_df["ID"] != orig["ID"]],
                        curr["Investment"],
                        curr["Profit Center"],
                        curr["GL Min"],
                        effective_max,
                    )
                    if not overlaps.empty:
                        overlap_warnings.append((orig["ID"], curr, overlaps))

                changes.append({
                    "ID": orig["ID"],
                    "Investment": curr["Investment"],
                    "Profit Center": curr["Profit Center"],
                    "Is Exclusion": bool(curr["Is Exclusion"]) if curr["Is Exclusion"] is not None else False,
                    "GL Min": curr["GL Min"],
                    "GL Max": effective_max,
                    "Enabled": bool(curr["Enabled"]),
                })

        if overlap_warnings:
            st.error(f"Found GL range overlaps in {len(overlap_warnings)} edit(s). Changes not saved.")
            for row_id, curr, overlaps in overlap_warnings:
                st.warning(
                    f"Row `{row_id}`: range `{curr['GL Min']}`-"
                    f"`{curr['GL Max']}` for Investment `{curr['Investment']}` / "
                    f"Profit Center `{curr['Profit Center']}` overlaps with:"
                )
                st.dataframe(
                    overlaps[["ID", "Investment", "Profit Center", "GL Min", "GL Max"]],
                    hide_index=True,
                    use_container_width=True,
                )
        elif changes:
            if deletes:
                st.warning(f"Disabling {len(deletes)} mapping(s): {', '.join(deletes)}")
            write_events(changes)
            st.cache_data.clear()
            st.rerun()
        else:
            st.info("No changes detected.")

with tab_add:
    st.subheader("Add New Mapping")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        new_investment = st.text_input("Investment")
    with col2:
        new_profit_center = st.text_input("Profit Center")
    with col3:
        new_min = st.text_input("GL Min")
    with col4:
        new_max = st.text_input("GL Max (leave blank to use GL Min)")

    new_is_exclusion = st.checkbox("Is Exclusion", value=False)

    if st.button("Add Mapping", type="primary"):
        effective_max = new_max if new_max else new_min
        if not all([new_investment, new_min]):
            st.error("Investment and GL Min are required.")
        elif effective_max < new_min:
            st.error("GL Max cannot be less than GL Min.")
        else:
            overlaps = find_overlaps(master_df, new_investment, new_profit_center, new_min, effective_max)
            if not overlaps.empty:
                st.error(
                    f"GL range `{new_min}`–`{effective_max}` overlaps with "
                    f"{len(overlaps)} existing mapping(s) for Investment `{new_investment}` / "
                    f"Profit Center `{new_profit_center}`:"
                )
                st.dataframe(
                    overlaps[["ID", "GL Min", "GL Max"]],
                    hide_index=True,
                    use_container_width=True,
                )
            else:
                new_id = str(uuid.uuid4())
                evt = {
                    "ID": new_id,
                    "Investment": new_investment,
                    "Profit Center": new_profit_center,
                    "Is Exclusion": new_is_exclusion,
                    "GL Min": new_min,
                    "GL Max": effective_max,
                    "Enabled": True,
                }
                write_events([evt])
                st.cache_data.clear()
                st.success(f"Added mapping: {new_id}. Refresh to see updates.")

with tab_bulk:
    st.subheader("Bulk Upload")
    st.caption("Upload a CSV file with columns: Investment, Profit Center, Is Exclusion, GL Min, GL Max")

    uploaded_file = st.file_uploader("Choose a CSV file", type="csv", key="bulk_upload")

    if uploaded_file is not None:
        try:
            upload_df = pd.read_csv(uploaded_file, dtype=str).fillna("")
            required_cols = {"Investment", "GL Min"}
            all_cols = {"Investment", "Profit Center", "Is Exclusion", "GL Min", "GL Max"}
            missing_cols = required_cols - set(upload_df.columns)
            if missing_cols:
                st.error(f"CSV is missing required columns: {', '.join(missing_cols)}")
            else:
                extra_cols = set(upload_df.columns) - all_cols
                if extra_cols:
                    st.warning(f"Ignoring unrecognized columns: {', '.join(extra_cols)}")
                upload_df = upload_df[[c for c in all_cols if c in upload_df.columns]]
                for col in all_cols:
                    if col not in upload_df.columns:
                        upload_df[col] = ""

                invalid_rows = upload_df[upload_df["Investment"].str.strip() == ""]
                if not invalid_rows.empty:
                    st.error(f"{len(invalid_rows)} row(s) are missing an Investment value.")
                else:
                    master_key = master_df["Investment"] + "|" + master_df["Profit Center"] + "|" + master_df["GL Min"].astype(str)
                    upload_key = upload_df["Investment"].str.strip() + "|" + upload_df["Profit Center"].str.strip() + "|" + upload_df["GL Min"].str.strip()
                    existing_mask = upload_key.isin(master_key)
                    num_updates = existing_mask.sum()
                    num_new = len(upload_df) - num_updates
                    if num_updates > 0:
                        st.info(f"{num_updates} row(s) match existing mappings and will be treated as updates.")
                    if num_new > 0:
                        st.info(f"{num_new} row(s) will be added as new mappings.")

                    st.dataframe(upload_df, hide_index=True, use_container_width=True)

                    if st.button("Confirm Bulk Upload", type="primary", key="bulk_confirm"):
                        events = []
                        key_to_id = dict(zip(master_key, master_df["ID"]))
                        for _, row in upload_df.iterrows():
                            k = row["Investment"].strip() + "|" + row["Profit Center"].strip() + "|" + row["GL Min"].strip()
                            existing_id = key_to_id.get(k)
                            gl_min = row["GL Min"].strip()
                            gl_max = row["GL Max"].strip() if row["GL Max"].strip() else gl_min
                            is_excl = row["Is Exclusion"].strip().lower() in ("true", "1", "yes")
                            events.append({
                                "ID": existing_id if existing_id else str(uuid.uuid4()),
                                "Investment": row["Investment"].strip(),
                                "Profit Center": row["Profit Center"].strip(),
                                "Is Exclusion": is_excl,
                                "GL Min": gl_min,
                                "GL Max": gl_max,
                                "Enabled": True,
                            })
                        total = len(events)
                        progress_bar = st.progress(0, text=f"Processing 0 of {total}...")
                        user = st.user.user_name
                        for i, evt in enumerate(events, 1):
                            evt["Changed By"] = user
                            payload = json.dumps(evt)
                            session.sql("""
                                INSERT INTO CONFIG.RAW.INVESTMENT_MAPPING_GL_EVENT_DATA
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
