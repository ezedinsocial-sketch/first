"""Patient management routes: admission, discharge, transfers, follow-ups."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from datetime import datetime, timedelta
import pandas as pd
import io
from flask import send_file

from models import (
    get_db_connection, get_patient, get_all_patients, get_room_transfers,
    get_active_transfer, get_distinct_surgeons, get_global_settings, count_occupied_beds,
    get_global_setting, update_patient
)
from validators import parse_date, valid_choice, required_text
from utils import login_required, admin_required, audit_log, patient_is_editable
from config import VALID_UNITS, ACTIVE_UNITS, VALID_DISCHARGE_OUTCOMES, VALID_BIOPSY_STATUSES, CAPACITY_KEY_MAP

patients = Blueprint('patients', __name__)


@patients.route('/')
def home():
    """Redirect to dashboard."""
    return redirect(url_for('patients.dashboard'))


@patients.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard with patient statistics."""
    conn = get_db_connection()
    all_patients = get_all_patients(conn)
    gs = get_global_settings(conn)
    
    # Organize patients by status
    active_in_patients = [p for p in all_patients if p['status'] == 'Admitted']
    tracked_out_patients = [p for p in all_patients if p['status'] in ['Discharged', 'Lost to Follow-up', 'Left Waiting List']]
    waiting_list_patients = [p for p in all_patients if p['status'] == 'Waiting List']
    surgeons = get_distinct_surgeons(conn)
    
    # Get capacity info
    tot_icu = gs.get('total_icu_beds', 6)
    tot_ward = gs.get('total_ward_beds', 26)
    tot_er = gs.get('total_er_beds', 10)
    tot_daycare = gs.get('total_daycare_beds', 10)
    
    icu_occupied = count_occupied_beds(conn, 'ICU 1')
    ward_occupied = count_occupied_beds(conn, 'Ward')
    er_occupied = count_occupied_beds(conn, 'ER')
    daycare_occupied = count_occupied_beds(conn, 'Day Care Surgery Unit')
    
    today = datetime.today()
    today_str = today.strftime("%Y-%m-%d")
    
    # Calculate alerts and metrics
    alert_2w_list, retry_2w_list, missed_2w_list = [], [], []
    alert_4w_list, retry_4w_list, missed_4w_list = [], [], []
    biopsy_alert_list, biopsy_delayed_list = [], []
    
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    total_ssi_cases = 0
    total_tracked_followups = 0
    total_discharged_patients = 0
    total_op_notes_completed = 0
    los_icu_accum, los_icu_count = 0, 0
    los_ward_accum, los_ward_count = 0, 0
    
    for p in all_patients:
        p['length_of_stay'] = 0
        p['biopsy_overdue'] = 'No'
        
        if p['date_admission']:
            try:
                adm_d = datetime.strptime(p['date_admission'], "%Y-%m-%d")
                end_d = datetime.strptime(p['date_discharge'], "%Y-%m-%d") if p['date_discharge'] else today
                p['length_of_stay'] = (end_d - adm_d).days
                
                if p['status'] in ['Discharged', 'Lost to Follow-up']:
                    if p['disposition_unit'] in ['ICU 1', 'ICU 2']:
                        los_icu_accum += p['length_of_stay']
                        los_icu_count += 1
                    else:
                        los_ward_accum += p['length_of_stay']
                        los_ward_count += 1
            except (TypeError, ValueError):
                pass
        
        # Biopsy tracking
        if p['status'] == 'Discharged' and p['biopsy_status'] in ['Yes', 'Pending', 'Delayed'] and p['biopsy_result_received'] != 'Yes':
            if p['biopsy_appointment_date'] and today_str > p['biopsy_appointment_date']:
                p['biopsy_overdue'] = 'Yes'
            
            if p['biopsy_status'] == 'Delayed':
                biopsy_delayed_list.append(p)
            else:
                biopsy_alert_list.append(p)
        
        # Follow-up tracking
        if p['status'] == 'Discharged' and p['discharge_status'] != 'Death':
            try:
                dis_d = datetime.strptime(p['date_discharge'], "%Y-%m-%d")
                days_since_discharge = (today - dis_d).days
                
                if p['call_2w_state'] == 'Pending' and days_since_discharge >= 14:
                    alert_2w_list.append(p)
                elif p['call_2w_state'] == 'Retry' and p['call_2w_retry_date']:
                    if today_str >= p['call_2w_retry_date']:
                        retry_2w_list.append(p)
                    else:
                        missed_2w_list.append(p)
                
                if p['call_4w_state'] == 'Pending' and days_since_discharge >= 28:
                    alert_4w_list.append(p)
                elif p['call_4w_state'] == 'Retry' and p['call_4w_retry_date']:
                    if today_str >= p['call_4w_retry_date']:
                        retry_4w_list.append(p)
                    else:
                        missed_4w_list.append(p)
            except (TypeError, ValueError):
                pass
            
            # Date range filtering for discharged patients
            in_range = True
            if start_date and p['date_discharge'] and p['date_discharge'] < start_date:
                in_range = False
            if end_date and p['date_discharge'] and p['date_discharge'] > end_date:
                in_range = False
            
            if in_range:
                total_discharged_patients += 1
                if p['op_note_complete'] == 'Yes':
                    total_op_notes_completed += 1
                if p['general_status_2w'] or p['general_condition_4w']:
                    total_tracked_followups += 1
                    if p['ssi_surveillance_2w'] in ['Clinical Signs', 'Diagnosis by a Clinician'] or p['ssi_surveillance_4w'] in ['Clinical Signs', 'Diagnosis by a Clinician']:
                        total_ssi_cases += 1
    
    # Calculate metrics
    ssi_rate = round((total_ssi_cases / total_tracked_followups) * 100, 1) if total_tracked_followups > 0 else 0.0
    avg_los_icu = round(los_icu_accum / los_icu_count, 1) if los_icu_count > 0 else 0.0
    avg_los_ward = round(los_ward_accum / los_ward_count, 1) if los_ward_count > 0 else 0.0
    op_note_completion_rate = round((total_op_notes_completed / total_discharged_patients) * 100, 1) if total_discharged_patients > 0 else 0.0
    
    totals = {
        'ward_max': tot_ward,
        'ward_avail': max(0, tot_ward - ward_occupied),
        'icu_max': tot_icu,
        'icu_avail': max(0, tot_icu - icu_occupied),
        'er_max': tot_er,
        'er_avail': max(0, tot_er - er_occupied),
        'daycare_max': tot_daycare,
        'daycare_avail': max(0, tot_daycare - daycare_occupied),
        'alert_2w_cnt': len(alert_2w_list) + len(retry_2w_list),
        'alert_4w_cnt': len(alert_4w_list) + len(retry_4w_list),
        'missed_2w_cnt': len(missed_2w_list),
        'missed_4w_cnt': len(missed_4w_list),
        'biopsy_alerts': len(biopsy_alert_list),
        'biopsy_delayed_cnt': len(biopsy_delayed_list),
        'ssi_rate': ssi_rate,
        'avg_los_icu': avg_los_icu,
        'avg_los_ward': avg_los_ward,
        'op_note_rate': op_note_completion_rate,
        'total_audited': total_tracked_followups
    }
    
    conn.close()
    
    return render_template(
        'index.html', view='dashboard', patients=all_patients,
        active_in_patients=active_in_patients, tracked_out_patients=tracked_out_patients,
        waiting_list_patients=waiting_list_patients, surgeons=surgeons, totals=totals, today_str=today_str,
        alert_2w_list=alert_2w_list, retry_2w_list=retry_2w_list, missed_2w_list=missed_2w_list,
        alert_4w_list=alert_4w_list, retry_4w_list=retry_4w_list, missed_4w_list=missed_4w_list,
        biopsy_alert_list=biopsy_alert_list, biopsy_delayed_list=biopsy_delayed_list,
        start_date=start_date, end_date=end_date
    )


@patients.route('/patient/<string:chart_number>')
@login_required
def patient_profile(chart_number):
    """View patient profile and edit details."""
    conn = get_db_connection()
    patient = get_patient(conn, chart_number)
    transfers = get_room_transfers(conn, chart_number)
    conn.close()
    
    if not patient:
        flash("Patient profile does not exist.", "danger")
        return redirect(url_for('patients.dashboard'))
    
    return render_template(
        'index.html', view='profile', p=dict(patient), transfers=transfers,
        today_str=datetime.today().strftime("%Y-%m-%d")
    )


@patients.route('/admit', methods=['POST'])
@login_required
def admit():
    """Admit a patient or move from waiting list."""
    form = request.form
    chart_num = form.get('chart_number', '').strip()
    disp_unit = form.get('disposition_unit')
    conn = get_db_connection()
    
    try:
        chart_num = required_text(chart_num, 'Chart number', 100)
        valid_choice(disp_unit, VALID_UNITS, 'disposition unit')
        admission_date = parse_date(form.get('date_admission'), 'Admission date')
        admission_officer = required_text(form.get('admission_officer'), 'Admission officer', 100)
        existing = get_patient(conn, chart_num)
        status = 'Waiting List' if disp_unit == 'Waiting List' else 'Admitted'
        
        if existing:
            patient_is_editable(existing)
            if existing['status'] != 'Waiting List' or status != 'Admitted':
                raise ValueError('This chart number already exists.')
            count_occupied_beds(conn, disp_unit, exclude_chart_number=chart_num)
            # Check capacity
            if count_occupied_beds(conn, disp_unit, exclude_chart_number=chart_num) >= get_global_setting(conn, CAPACITY_KEY_MAP.get(disp_unit, 'total_ward_beds')):
                raise ValueError(f"No {disp_unit} bed is available.")
            
            conn.execute('''
                UPDATE patients SET disposition_unit=?, status='Admitted', date_admission=?, admission_officer=?,
                waiting_list_leave_reason=NULL WHERE chart_number=?
            ''', (disp_unit, admission_date.isoformat(), admission_officer, chart_num))
            action = 'waiting_list_patient_admitted'
        else:
            full_name = required_text(form.get('full_name'), 'Full patient name', 200)
            if status == 'Admitted':
                if count_occupied_beds(conn, disp_unit) >= get_global_setting(conn, CAPACITY_KEY_MAP.get(disp_unit, 'total_ward_beds')):
                    raise ValueError(f"No {disp_unit} bed is available.")
            
            conn.execute('''
                INSERT INTO patients (
                    chart_number, full_name, phone, gender, admission_type, surgeon_name, diagnosis, date_admission, admission_officer,
                    history_done, pe_done, ix_done, pre_anesthesia_done, disposition_unit, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                chart_num, full_name, form.get('phone', '').strip(), form.get('gender'), form.get('admission_type'),
                form.get('surgeon_name', '').strip(), form.get('diagnosis', '').strip(), admission_date.isoformat(), admission_officer,
                form.get('history_done', 'No'), form.get('pe_done', 'No'), form.get('ix_done', 'No'), form.get('pre_anesthesia_done', 'No'),
                disp_unit, status
            ))
            action = 'patient_admitted' if status == 'Admitted' else 'patient_added_to_waiting_list'
        
        if status == 'Admitted':
            conn.execute("DELETE FROM room_transfers WHERE chart_number=?", (chart_num,))
            conn.execute("INSERT INTO room_transfers (chart_number, room_service, start_date) VALUES (?, ?, ?)",
                         (chart_num, disp_unit, admission_date.isoformat()))
        
        audit_log(conn, action, chart_num, f"unit={disp_unit}")
        conn.commit()
        flash(f"Admission registry updated for chart {chart_num}.", "success")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    finally:
        conn.close()
    
    return redirect(url_for('patients.dashboard'))


@patients.route('/discharge', methods=['POST'])
@login_required
def discharge():
    """Discharge a patient."""
    form = request.form
    chart = form.get('discharge_chart_number', '').strip()
    conn = get_db_connection()
    
    try:
        status_type = valid_choice(form.get('discharge_status_selector', 'Discharged'), {'Discharged', 'Lost to Follow-up'}, 'discharge type')
        discharge_date = parse_date(form.get('date_discharge'), 'Discharge date')
        procedure_done = required_text(form.get('procedure_done'), 'Procedure', 500)
        discharge_officer = required_text(form.get('discharge_officer'), 'Discharge officer', 100)
        discharge_status = valid_choice(form.get('discharge_status'), VALID_DISCHARGE_OUTCOMES, 'discharge outcome')
        op_note_complete = valid_choice(form.get('op_note_complete', 'No'), {'Yes', 'No'}, 'operation-note status')
        biopsy_status = valid_choice(form.get('biopsy_status'), VALID_BIOPSY_STATUSES - {'Delayed'}, 'biopsy status')
        biopsy_date = parse_date(form.get('biopsy_appointment_date'), 'Biopsy date', required=False)
        appointment_date = parse_date(form.get('appointment_date'), 'Return date', required=False)
        patient = get_patient(conn, chart)
        patient_is_editable(patient, 'Admitted')
        
        if discharge_date < parse_date(patient['date_admission'], 'Admission date'):
            raise ValueError('Discharge date cannot be before admission date.')
        
        reason_lost = required_text(form.get('lost_to_followup_reason'), 'Lost reason', 500) if status_type == 'Lost to Follow-up' else None
        high_risk = 1 if form.get('high_risk_ssi') == '1' else 0
        
        active_trans = get_active_transfer(conn, chart)
        if active_trans:
            transfer_start = parse_date(active_trans['start_date'], 'Current room start date')
            if discharge_date < transfer_start:
                raise ValueError('Discharge date cannot be before current room start date.')
            conn.execute("UPDATE room_transfers SET end_date=?, days_spent=? WHERE id=?",
                         (discharge_date.isoformat(), (discharge_date - transfer_start).days, active_trans['id']))
        
        conn.execute('''
            UPDATE patients SET date_discharge=?, procedure_done=?, discharge_status=?, op_note_complete=?,
            biopsy_status=?, biopsy_appointment_date=?, appointment_date=?, discharge_officer=?,
            status=?, lost_to_followup_reason=?, call_2w_state='Pending', call_2w_retry_date=NULL,
            call_2w_unreachable_reason=NULL, call_4w_state='Pending', call_4w_retry_date=NULL,
            call_4w_unreachable_reason=NULL, high_risk_ssi=? WHERE chart_number=?
        ''', (discharge_date.isoformat(), procedure_done, discharge_status, op_note_complete, biopsy_status,
              biopsy_date.isoformat() if biopsy_date else None, appointment_date.isoformat() if appointment_date else None,
              discharge_officer, status_type, reason_lost, high_risk, chart))
        
        audit_log(conn, 'patient_discharged', chart, f"status={status_type}; outcome={discharge_status}")
        conn.commit()
        flash('Discharge record saved.', 'primary')
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    finally:
        conn.close()
    
    return redirect(url_for('patients.dashboard'))


@patients.route('/change_room', methods=['POST'])
@login_required
def change_room():
    """Transfer patient to different unit."""
    chart = request.form.get('chart_number', '').strip()
    new_unit = request.form.get('new_disposition_unit')
    conn = get_db_connection()
    
    try:
        valid_choice(new_unit, ACTIVE_UNITS, "destination unit")
        change_date = parse_date(request.form.get('change_date'), "Reallocation date")
        patient = get_patient(conn, chart)
        patient_is_editable(patient, "Admitted")
        
        # Check capacity
        if count_occupied_beds(conn, new_unit, exclude_chart_number=chart) >= get_global_setting(conn, CAPACITY_KEY_MAP.get(new_unit, 'total_ward_beds')):
            raise ValueError(f"No {new_unit} bed is available.")
        
        admission_date = parse_date(patient['date_admission'], "Admission date")
        if change_date < admission_date:
            raise ValueError("Reallocation date cannot be before admission date.")
        
        active_trans = get_active_transfer(conn, chart)
        if active_trans:
            start_date = parse_date(active_trans['start_date'], "Current room start date")
            if change_date < start_date:
                raise ValueError("Reallocation date cannot be before current room start date.")
            days = (change_date - start_date).days
            conn.execute("UPDATE room_transfers SET end_date=?, days_spent=? WHERE id=?", (change_date.isoformat(), days, active_trans['id']))
        
        conn.execute("INSERT INTO room_transfers (chart_number, room_service, start_date) VALUES (?, ?, ?)", (chart, new_unit, change_date.isoformat()))
        conn.execute("UPDATE patients SET disposition_unit=? WHERE chart_number=?", (new_unit, chart))
        audit_log(conn, "room_changed", chart, f"unit={new_unit}; date={change_date.isoformat()}")
        conn.commit()
        flash(f"Room allocation updated to: {new_unit}", "info")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    finally:
        conn.close()
    
    return redirect(url_for('patients.patient_profile', chart_number=chart))


@patients.route('/unlock_profile/<string:chart_number>', methods=['POST'])
@admin_required
def unlock_profile(chart_number):
    """Unlock a patient file for re-editing."""
    conn = get_db_connection()
    patient = get_patient(conn, chart_number)
    
    if not patient:
        flash("Patient profile does not exist.", "danger")
    else:
        conn.execute("UPDATE patients SET file_locked=0 WHERE chart_number=?", (chart_number,))
        audit_log(conn, "patient_file_unlocked", chart_number)
        conn.commit()
        flash("File unlocked for re-editing.", "warning")
    
    conn.close()
    return redirect(url_for('patients.patient_profile', chart_number=chart_number))


@patients.route('/log_pipeline_call', methods=['POST'])
@login_required
def log_pipeline_call():
    """Log follow-up call (2-week or 4-week)."""
    chart = request.form.get('chart_number', '').strip()
    call_type = request.form.get('call_type')
    reachable = request.form.get('reachable')
    conn = get_db_connection()
    
    try:
        valid_choice(call_type, {'2w', '4w'}, 'follow-up type')
        valid_choice(reachable, {'Yes', 'No'}, 'reachability status')
        patient = get_patient(conn, chart)
        patient_is_editable(patient, 'Discharged')
        
        if patient['discharge_status'] == 'Death':
            raise ValueError("Follow-up calls cannot be recorded for deceased patients.")
        
        retry_dt = (datetime.today() + timedelta(days=7)).strftime("%Y-%m-%d")
        
        if reachable == 'No':
            reason = valid_choice(request.form.get('unreachable_reason'), {
                'Phone not working/Switched off', 'Patient or attendant not available',
                'Language barrier Structural issue', 'Other'
            }, 'unreachable reason')
            other_text = request.form.get('unreachable_reason_other', '').strip()
            if reason == 'Other' and not other_text:
                raise ValueError("Please provide detailed reason.")
            final_reason = f"Other: {other_text}" if reason == 'Other' else reason
            
            if call_type == '2w':
                conn.execute('''
                    UPDATE patients SET call_2w_state='Retry', call_2w_retry_date=?,
                    call_2w_unreachable_reason=?, general_status_2w='Not Available',
                    ssi_surveillance_2w='Not Available' WHERE chart_number=?
                ''', (retry_dt, final_reason, chart))
            else:
                conn.execute('''
                    UPDATE patients SET call_4w_state='Retry', call_4w_retry_date=?,
                    call_4w_unreachable_reason=?, general_condition_4w='Not Available',
                    ssi_surveillance_4w='Not Available' WHERE chart_number=?
                ''', (retry_dt, final_reason, chart))
            
            audit_log(conn, f"{call_type}_followup_retry", chart, f"retry_date={retry_dt}; reason={final_reason}")
            flash(f"Patient unreachable. Follow-up retry due on {retry_dt}.", "warning")
        
        elif call_type == '2w':
            rating = int(request.form.get('hospital_rating_2w', 0))
            if rating not in range(1, 6):
                raise ValueError("Rating must be from 1 to 5.")
            
            ssi_status = valid_choice(request.form.get('ssi_surveillance_2w'), {
                'No Sign of Infection', 'Clinical Signs', 'Diagnosis by a Clinician'
            }, '2-week SSI status')
            
            conn.execute('''
                UPDATE patients SET general_status_2w=?, deterioration_details=?, hospital_rating_2w=?,
                ssi_surveillance_2w=?, caller_name_2w=?, call_2w_state='Cleared',
                call_2w_retry_date=NULL, call_2w_unreachable_reason=NULL WHERE chart_number=?
            ''', (request.form.get('general_status_2w', '').strip(), request.form.get('deterioration_details', '').strip(),
                  rating, ssi_status, request.form.get('caller_name_2w', '').strip(), chart))
            
            audit_log(conn, '2w_followup_completed', chart)
            flash("2-week verification record saved.", "success")
        
        else:  # 4w
            ssi_status = valid_choice(request.form.get('ssi_surveillance_4w'), {
                'No Sign of Infection', 'Clinical Signs', 'Diagnosis by a Clinician'
            }, '4-week SSI status')
            
            conn.execute('''
                UPDATE patients SET general_condition_4w=?, service_complaints=?, physician_opinion=?,
                nursing_opinion=?, price_worthiness=?, ssi_surveillance_4w=?, caller_name_4w=?,
                call_4w_state='Cleared', call_4w_retry_date=NULL, call_4w_unreachable_reason=NULL,
                file_locked=1 WHERE chart_number=?
            ''', (request.form.get('general_condition_4w', '').strip(), request.form.get('service_complaints', '').strip(),
                  request.form.get('physician_opinion', '').strip(), request.form.get('nursing_opinion', '').strip(),
                  request.form.get('price_worthiness', '').strip(), ssi_status,
                  request.form.get('caller_name_4w', '').strip(), chart))
            
            audit_log(conn, '4w_followup_completed_and_locked', chart)
            flash("4-week quality audit finalized and record locked.", "dark")
        
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    finally:
        conn.close()
    
    return redirect(url_for('patients.patient_profile', chart_number=chart))


@patients.route('/leave_waiting_list', methods=['POST'])
@login_required
def leave_waiting_list():
    """Remove patient from waiting list."""
    chart = request.form.get('chart_number', '').strip()
    conn = get_db_connection()
    
    try:
        patient = get_patient(conn, chart)
        patient_is_editable(patient, 'Waiting List')
        
        reason = valid_choice(request.form.get('waiting_list_leave_reason'), {
            'Went to other hospital', 'Will come back', 'Financial reason structural issue',
            'Dissatisfied with lack of bed space asset availability', 'Other'
        }, 'waiting-list leave reason')
        
        other_text = request.form.get('waiting_list_leave_reason_other', '').strip()
        if reason == 'Other' and not other_text:
            raise ValueError('Please provide detailed reason.')
        
        final_reason = f"Other: {other_text}" if reason == 'Other' else reason
        conn.execute("UPDATE patients SET status='Left Waiting List', waiting_list_leave_reason=? WHERE chart_number=?", (final_reason, chart))
        audit_log(conn, 'patient_left_waiting_list', chart, final_reason)
        conn.commit()
        flash("Patient removed from waiting list.", "info")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    finally:
        conn.close()
    
    return redirect(url_for('patients.dashboard'))


@patients.route('/update_biopsy_result', methods=['POST'])
@login_required
def update_biopsy_result():
    """Update biopsy status and results."""
    chart = request.form.get('biopsy_chart_number', '').strip()
    conn = get_db_connection()
    
    try:
        patient = get_patient(conn, chart)
        patient_is_editable(patient)
        
        status_selection = valid_choice(request.form.get('biopsy_status'), VALID_BIOPSY_STATUSES, 'biopsy status')
        result_text = required_text(request.form.get('biopsy_final_result'), 'Biopsy result', 5000)
        received = valid_choice(request.form.get('biopsy_result_received', 'No'), {'Yes', 'No'}, 'biopsy-received status')
        delay_reason = request.form.get('biopsy_delay_reason', '').strip()
        
        if status_selection == 'Delayed':
            if not delay_reason:
                raise ValueError('Please provide biopsy delay reason.')
            received = 'No'
        else:
            delay_reason = ''
        
        conn.execute('''
            UPDATE patients SET biopsy_status=?, biopsy_final_result=?, biopsy_result_received=?, biopsy_delay_reason=?
            WHERE chart_number=?
        ''', (status_selection, result_text, received, delay_reason, chart))
        
        audit_log(conn, 'biopsy_status_updated', chart, f"status={status_selection}; received={received}")
        conn.commit()
        flash('Biopsy status updated.', 'success')
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), 'danger')
    finally:
        conn.close()
    
    return redirect(url_for('patients.dashboard'))


@patients.route('/export_excel')
@admin_required
def export_excel():
    """Export patient data to Excel."""
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    try:
        start = parse_date(start_date, 'Start date', required=False)
        end = parse_date(end_date, 'End date', required=False)
        if start and end and start > end:
            raise ValueError('Start date cannot be after end date.')
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('patients.dashboard'))
    
    conn = get_db_connection()
    df_raw = pd.read_sql_query("SELECT * FROM patients", conn)
    audit_log(conn, 'patient_data_exported', details=f"start={start_date or 'all'}; end={end_date or 'all'}")
    conn.commit()
    conn.close()
    
    today_str = datetime.today().strftime("%Y-%m-%d")
    
    def check_overdue(row):
        if row['status'] == 'Discharged' and row['biopsy_status'] in ['Yes', 'Pending', 'Delayed'] and row['biopsy_result_received'] != 'Yes':
            if row['biopsy_appointment_date'] and today_str > row['biopsy_appointment_date']:
                return 'Yes'
        return 'No'
    
    df_raw['Is_Biopsy_Overdue'] = df_raw.apply(check_overdue, axis=1)
    
    if start_date:
        df_raw = df_raw[(df_raw['date_discharge'].isna()) | (df_raw['date_discharge'] >= start_date)]
    if end_date:
        df_raw = df_raw[(df_raw['date_discharge'].isna()) | (df_raw['date_discharge'] <= end_date)]
    
    for column in df_raw.select_dtypes(include='object').columns:
        df_raw[column] = df_raw[column].map(
            lambda value: f"'{value}" if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value
        )
    
    df_admitted = df_raw[df_raw['status'] == 'Admitted']
    df_discharged = df_raw[df_raw['status'] == 'Discharged']
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_raw.to_excel(writer, index=False, sheet_name='Master_Surveillance_Data')
        df_admitted.to_excel(writer, index=False, sheet_name='Admitted_Patients')
        df_discharged.to_excel(writer, index=False, sheet_name='Discharged_Patients')
    
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"Yanet_Surveillance_{datetime.today().strftime('%Y%m%d')}.xlsx")
