import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';


const formatAngle = (value) => value == null ? '—' : `${value}°`;

export function TrackerTitle() {
  return <div className="tracker-title"><h1>CAREE Exercise Tracker</h1></div>;
}

export function Divider() {
  return <div className="divider" role="separator" />;
}

export function TrackerDescription() {
  return (
    <div className="tracker-description">
      <p>Monitor student exercise repetitions and joint angles with live camera tracking.
        Add students, link visible detections, and keep their full and partial rep totals even when tracking is lost.</p>
    </div>
  );
}

export function PreviewToggle() {
  const [enabled, setEnabled] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const revision = useRef(0);

  useEffect(() => {
    let stopped = false;
    let timer;
    const controller = new AbortController();
    async function refresh() {
      const currentRevision = revision.current;
      try {
        const response = await fetch('/api/preview', { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error('Unable to read preview status.');
        const data = await response.json();
        if (!stopped && currentRevision === revision.current) setEnabled(data.enabled);
      } catch (error) {
        if (!stopped) setError('Unable to read preview status.');
      } finally {
        if (!stopped) timer = setTimeout(refresh, 1000);
      }
    }
    refresh();
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, []);

  async function toggle() {
    revision.current += 1;
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/preview', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !enabled }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Unable to change preview.');
      revision.current += 1;
      setEnabled(data.enabled);
    } catch (error) {
      setError(error.message || 'Unable to change preview.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="preview-controls">
      <button className="preview-toggle" type="button" onClick={toggle}
        disabled={busy || enabled === null} aria-pressed={enabled === true}>
        {busy ? 'Updating preview…' : enabled ? 'Hide OpenCV camera' : 'Show OpenCV camera'}
      </button>
      {error && <span className="preview-error" role="alert">{error}</span>}
    </div>
  );
}

export function EditableStudentName({ student, onRename }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(student.name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const cancelled = useRef(false);
  const submitting = useRef(false);

  async function save() {
    if (cancelled.current || submitting.current) return;
    const name = draft.trim();
    if (!name) {
      setError('Enter a name.');
      return;
    }
    if (name === student.name) {
      setEditing(false);
      return;
    }
    submitting.current = true;
    setSaving(true);
    setError('');
    try {
      const response = await fetch(`/api/students/${student.id}/name`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Unable to save name.');
      onRename(result.id, result.name);
      setEditing(false);
    } catch (error) {
      setError(error.message || 'Unable to save name.');
    } finally {
      submitting.current = false;
      setSaving(false);
    }
  }

  return (
    <div className="editable-name">
      {editing ? (
        <input
          className="name-input"
          aria-label={`Name for person ${student.id}`}
          aria-invalid={Boolean(error)}
          autoFocus
          maxLength={80}
          value={draft}
          readOnly={saving}
          onFocus={event => event.target.select()}
          onChange={event => setDraft(event.target.value)}
          onBlur={save}
          onKeyDown={event => {
            if (event.key === 'Enter') { event.preventDefault(); save(); }
            if (event.key === 'Escape' && !saving) {
              cancelled.current = true;
              setEditing(false);
              setError('');
            }
          }}
        />
      ) : (
        <button className="name-button" type="button" title={`Rename person ${student.id}`}
          onClick={() => {
            cancelled.current = false;
            setDraft(student.name);
            setError('');
            setEditing(true);
          }}>
          {student.name}
        </button>
      )}
      {error && <span className="name-error" role="alert">{error}</span>}
    </div>
  );
}

async function mutate(url, method, body) {
  const response = await fetch(url, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Unable to save changes.');
  return result;
}

function TableHeader({ columns }) {
  return <div className="header" role="row">{columns.map(label => <div key={label} role="columnheader">{label}</div>)}</div>;
}

function EmptyRow({ children, columns }) {
  return <div role="row"><div role="cell" aria-colspan={columns} className="detection-message">{children}</div></div>;
}

function AddStudent({ onChange }) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function add(event) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true); setError('');
    try {
      await mutate('/api/students', 'POST', { name: name.trim() });
      setName(''); onChange();
    } catch (error) { setError(error.message); }
    finally { setBusy(false); }
  }
  return <form className="add-student" onSubmit={add}>
    <input aria-label="New student name" placeholder="Student name" value={name} maxLength={80}
      onChange={event => setName(event.target.value)} required />
    <button className="reassign" disabled={busy || !name.trim()}>{busy ? 'Adding…' : 'Add student'}</button>
    {error && <span className="name-error" role="alert">{error}</span>}
  </form>;
}

export function EditableRepCount({ student, field, onChange, onError }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const original = useRef(0);
  const cancelled = useRef(false);
  const submitting = useRef(false);
  const label = field === 'reps' ? 'Total reps' : 'Partial reps';

  async function save() {
    if (cancelled.current || submitting.current) return;
    const text = draft.trim();
    const value = Number(text);
    if (!/^\d+$/.test(text) || !Number.isSafeInteger(value) || value > 2147483647) {
      setInvalid(true);
      onError(`${label}: invalid input.`);
      return;
    }
    // Live totals may advance while editing: an untouched input must not reset them.
    if (value === original.current) { setEditing(false); onError(''); return; }
    submitting.current = true; setSaving(true); setInvalid(false); onError('');
    try {
      await mutate(`/api/students/${student.id}/counts`, 'PATCH', { mode: student.mode, field, value });
      setEditing(false); onChange();
    } catch (error) { onError(error.message); }
    finally { submitting.current = false; setSaving(false); }
  }

  return editing ? <input className="name-input count-input" type="text" inputMode="numeric"
    autoFocus value={draft} readOnly={saving} aria-invalid={invalid}
    aria-label={`${label} for ${student.name} (${student.mode})`}
    onFocus={event => event.target.select()}
    onChange={event => { setDraft(event.target.value); setInvalid(false); }}
    onBlur={save}
    onKeyDown={event => {
      if (event.key === 'Enter') { event.preventDefault(); save(); }
      if (event.key === 'Escape' && !saving) { cancelled.current = true; setEditing(false); onError(''); }
    }} /> : <button type="button" className="name-button count-button"
      aria-label={`Edit ${label.toLowerCase()} for ${student.name}`} title={`Edit ${label.toLowerCase()}`}
      onClick={() => {
        original.current = student[field]; setDraft(String(student[field]));
        cancelled.current = false; setInvalid(false); onError(''); setEditing(true);
      }}>{student[field]}</button>;
}

export function LoggedStudentRow({ student, onChange, onError }) {
  const [busy, setBusy] = useState(false);
  async function changeMode(mode) {
    setBusy(true); onError('');
    try {
      await mutate(`/api/students/${student.id}/mode`, 'PATCH', { mode });
      onChange();
    } catch (error) { onError(error.message); }
    finally { setBusy(false); }
  }
  async function remove() {
    setBusy(true); onError('');
    try {
      await mutate(`/api/students/${student.id}`, 'DELETE');
      onChange();
    } catch (error) { onError(error.message); }
    finally { setBusy(false); }
  }
  return <div className="student-row" role="row">
    <div role="cell">{student.id}</div>
    <div role="cell"><EditableStudentName student={student} onRename={onChange} /></div>
    <div role="cell" className="rep-value"><EditableRepCount key={`${student.mode}-reps`} student={student} field="reps" onChange={onChange} onError={onError} /></div>
    <div role="cell" className="rep-value"><EditableRepCount key={`${student.mode}-partial`} student={student} field="partial" onChange={onChange} onError={onError} /></div>
    <div role="cell">
      <select className="exercise-select" value={student.mode} disabled={busy}
        aria-label={`Exercise for ${student.name}`} onChange={event => changeMode(event.target.value)}>
        <option value="arms">Arms</option><option value="legs">Legs</option>
      </select>
    </div>
    <div role="cell">
      <button type="button" className="reassign remove-student" disabled={busy}
        aria-label={`Remove ${student.name} from the log`} title="Remove student and their saved rep totals"
        onClick={remove}>Remove</button>
    </div>
  </div>;
}

export function LoggedStudents({ students, onChange }) {
  const [error, setError] = useState('');
  return <section className="list-section" aria-labelledby="logged-title">
    <div className="list-heading"><h2 id="logged-title">Logged students</h2><AddStudent onChange={onChange} /></div>
    <div className="student-list logged-list" role="table" aria-label="Logged students" tabIndex={0}>
      <TableHeader columns={['Student ID', 'Name', 'Total reps', 'Partial reps', 'Exercise', 'Remove']} />
      <div className="students" role="rowgroup">
        {students.map(student => <LoggedStudentRow key={student.id} student={student} onChange={onChange} onError={setError} />)}
        {!students.length && <EmptyRow columns={6}>Add a student to start logging exercise reps.</EmptyRow>}
      </div>
    </div>
    {error && <p className="name-error" role="alert">{error}</p>}
  </section>;
}

function DetectionLink({ detection, students, onChange, onError }) {
  const [busy, setBusy] = useState(false);
  async function link(studentId) {
    setBusy(true); onError('');
    try {
      await mutate(`/api/detections/${detection.id}/link`, 'PUT', { student_id: studentId });
      onChange();
    } catch (error) { onError(error.message); }
    finally { setBusy(false); }
  }
  return <div className="link-controls">
    {detection.student_id == null ? (
      <select className="exercise-select" aria-label={`Student for detection ${detection.id}`}
        value="" disabled={busy || !students.length} onChange={event => { if (event.target.value) link(Number(event.target.value)); }}>
        <option value="" disabled>{busy ? 'Linking…' : students.length ? 'Choose student' : 'No students'}</option>
        {students.map(student => <option key={student.id} value={student.id}>
          {student.name} (#{student.id}){student.detection_id != null ? ' — linked' : ''}
        </option>)}
      </select>
    ) : (
      <button type="button" className="reassign" disabled={busy} onClick={() => link(null)}>{busy ? 'Unlinking…' : 'Unlink'}</button>
    )}
  </div>;
}

export function VisibleDetectionRow({ detection, students, onChange, onError }) {
  const stage = mode => detection[mode].status === 'Lost tracking' ? 'Not visible' : detection[mode].stage;
  const status = detection.mode ? `${detection.mode}: ${stage(detection.mode)}` : `Arms: ${stage('arms')} · Legs: ${stage('legs')}`;
  return <div className="student-row" role="row">
    <div role="cell">{detection.id}</div>
    <div role="cell" className="student-name">{detection.name}</div>
    <div role="cell" className="angle-value">{formatAngle(detection.arms.left)} / {formatAngle(detection.arms.right)}</div>
    <div role="cell" className="angle-value">{formatAngle(detection.legs.left)} / {formatAngle(detection.legs.right)}</div>
    <div role="cell" className="rep-status" title={status}>
      {status}
    </div>
    <div role="cell"><DetectionLink detection={detection} students={students} onChange={onChange} onError={onError} /></div>
  </div>;
}

export function VisibleDetections({ detections, students, message, onChange }) {
  const [error, setError] = useState('');
  return <section className="list-section" aria-labelledby="visible-title">
    <div className="list-heading"><h2 id="visible-title">Visible detections</h2></div>
    <div className="student-list visible-list" role="table" aria-label="Visible detections" tabIndex={0}>
      <TableHeader columns={['Temporary ID', 'Linked student', 'Arm angles L/R', 'Leg angles L/R', 'Rep status', 'Link']} />
      <div className="students" role="rowgroup">
        {detections.map(detection => <VisibleDetectionRow key={detection.id} detection={detection} students={students} onChange={onChange} onError={setError} />)}
        {!detections.length && <EmptyRow columns={6}>{message}</EmptyRow>}
      </div>
    </div>
    {error && <p className="name-error" role="alert">{error}</p>}
  </section>;
}

export function StudentDashboard() {
  const [data, setData] = useState({ status: 'starting', students: [], detections: [], error: null });
  const [revision, setRevision] = useState(0);
  const refresh = () => setRevision(value => value + 1);
  useEffect(() => {
    let stopped = false;
    let timer;
    let controller;
    async function poll() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch('/api/students', { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error('Unable to load tracking data.');
        const snapshot = await response.json();
        if (!stopped) setData(snapshot);
      } catch (error) {
        if (!stopped) setData(current => ({ ...current, detections: [], status: 'error', error: 'Unable to get camera data. Saved student totals are unchanged.' }));
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(poll, 250);
      }
    }
    poll();
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
  }, [revision]);
  const message = data.error || (data.status === 'starting' ? 'Starting camera and pose model…' : 'No people visible. Logged students and their totals are saved.');
  return <>
    <LoggedStudents students={data.students} onChange={refresh} />
    <VisibleDetections detections={data.detections} students={data.students} message={message} onChange={refresh} />
  </>;
}

function App() {
  return <div className="tracker-page">
    <TrackerTitle /><Divider /><TrackerDescription /><Divider />
    <PreviewToggle /><StudentDashboard />
  </div>;
}
createRoot(document.getElementById('root')).render(<App />);
