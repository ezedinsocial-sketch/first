/**
 * Main application JavaScript
 */

// Modal Functions
function launchDischargeModal(chartNum) {
    document.getElementById('discharge_chart_hidden').value = chartNum;
}

function populateBiopsyModal(chartNum, currentStatus) {
    document.getElementById('biopsy_chart_hidden').value = chartNum;
    document.getElementById('biopsy_status_select_modal').value = currentStatus;
    toggleBiopsyDelayReasonField();
}

function triggerWaitingAllocationModal(chartNum, fullName) {
    document.getElementById('alloc_chart_hidden').value = chartNum;
    document.getElementById('alloc_name_hidden').value = fullName;
    document.getElementById('alloc_name_span').innerText = fullName + " (" + chartNum + ")";
}

function triggerWaitingLeaveModal(chartNum) {
    document.getElementById('waiting_leave_chart_hidden').value = chartNum;
}

// Tab Navigation
function switchToFollowUpTab() {
    var triggerEl = document.querySelector('#dashboardTabs button[data-bs-target="#followups-pane"]');
    if(triggerEl) {
        bootstrap.Tab.getInstance(triggerEl || new bootstrap.Tab(triggerEl)).show();
    }
}

// ICU Sub-unit Selection
function evaluateSubUnitDisplay() {
    var choice = document.getElementById('unitSelectionField').value;
    var subModule = document.getElementById('icuSuboptionsModule');
    if(choice === 'ICU') {
        subModule.classList.remove('d-none');
    } else {
        subModule.classList.add('d-none');
    }
}

function prepareSubmissionParameters(event, formElement) {
    var choice = document.getElementById('unitSelectionField').value;
    if(choice === 'ICU') {
        event.preventDefault();
        var targetValue = document.getElementById('icuSubSelectionTarget').value;
        document.getElementById('unitSelectionField').innerHTML += `<option value="${targetValue}" selected>${targetValue}</option>`;
        document.getElementById('unitSelectionField').value = targetValue;
        formElement.submit();
    }
}

// Form Field Toggles
function toggleLostToFollowupReasonBox() {
    var sel = document.getElementById('statusSelector').value;
    var wrapper = document.getElementById('lost_reason_wrapper');
    if(sel === 'Lost to Follow-up') {
        wrapper.classList.remove('d-none');
    } else {
        wrapper.classList.add('d-none');
    }
}

function toggleBiopsyDelayReasonField() {
    var sel = document.getElementById('biopsy_status_select_modal').value;
    var wrapper = document.getElementById('biopsy_delay_reason_wrapper');
    if(sel === 'Delayed') {
        wrapper.classList.remove('d-none');
    } else {
        wrapper.classList.add('d-none');
    }
}

function toggleWaitingOtherReasonField() {
    var sel = document.getElementById('waitLeaveSelect').value;
    var wrapper = document.getElementById('waiting_leave_other_wrapper');
    if(sel === 'Other') {
        wrapper.classList.remove('d-none');
    } else {
        wrapper.classList.add('d-none');
    }
}

function toggleReachabilityFields(prefix) {
    var state = document.getElementById('reach' + prefix).value;
    var reachDiv = document.getElementById('form_' + prefix + '_reachable');
    var unreachDiv = document.getElementById('form_' + prefix + '_unreachable');
    if (state === 'Yes') {
        reachDiv.classList.remove('d-none');
        unreachDiv.classList.add('d-none');
    } else if (state === 'No') {
        reachDiv.classList.add('d-none');
        unreachDiv.classList.remove('d-none');
        toggleOtherReason(prefix);
    } else {
        reachDiv.classList.add('d-none');
        unreachDiv.classList.add('d-none');
    }
}

function toggleOtherReason(prefix) {
    var sel = document.getElementById('reason' + prefix).value;
    var wrapper = document.getElementById('other_' + prefix + '_div');
    if(sel === 'Other') {
        wrapper.classList.remove('d-none');
    } else {
        wrapper.classList.add('d-none');
    }
}
