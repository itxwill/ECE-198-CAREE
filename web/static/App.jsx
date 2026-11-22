import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';

const columns = ['Student', 'Exercise', 'Reps', 'Active angle', 'Left angle', 'Right angle', 'Status', 'Assignment'];
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
        Switch between arms and legs, and click a student’s name to rename them.</p>
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

function ReassignStudent({ student, students }) {
  const [editing, setEditing] = useState(false);
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const candidates = students.filter(person => person.tracked && person.id !== student.id);

  async function reassign() {
    setBusy(true);
    setError('');
    try {
      const response = await fetch(`/api/students/${student.id}/reassign`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: Number(target) }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Unable to reassign.');
      setEditing(false);
      setTarget('');
    } catch (error) {
      setError(error.message);
    } finally { setBusy(false); }
  }

  if (student.tracked) return <button type="button" className="reassign" disabled title="Available when this student loses tracking">Reassign</button>;
  return (
    <div className="reassign-controls">
      {!editing ? (
        <button type="button" className="reassign" onClick={() => setEditing(true)}>Reassign</button>
      ) : (
        <>
          <select value={target} onChange={event => setTarget(event.target.value)} disabled={busy}
            aria-label={`New detection for ${student.name}`}>
            <option value="">Choose ID</option>
            {candidates.map(person => <option key={person.id} value={person.id}>{person.name} (ID {person.id})</option>)}
          </select>
          <button type="button" className="reassign" disabled={busy || !candidates.some(person => String(person.id) === target)}
            title="Keep this student's name and add the selected detection's completed reps to their saved totals"
            onClick={reassign}>{busy ? 'Linking…' : 'Link'}</button>
          <button type="button" className="reassign" disabled={busy} onClick={() => { setEditing(false); setError(''); }}>Cancel</button>
          {candidates.length === 0 && <span className="name-error">No visible people.</span>}
        </>
      )}
      {error && <span className="name-error" role="alert">{error}</span>}
    </div>
  );
}

export function StudentRow({ student, students, onRename }) {
  const [mode, setMode] = useState('arms');
  const metrics = student[mode];
  const lost = metrics.status === 'Lost tracking';

  return (
    <div role="row" className={`student-row${lost ? ' student-row--lost' : ''}`}>
      <div role="cell" className="student-name">
        <EditableStudentName student={student} onRename={onRename} />
      </div>
      <div role="cell">
        <select
          className="exercise-select"
          value={mode}
          onChange={event => setMode(event.target.value)}
          aria-label={`Exercise for ${student.name}`}
        >
          <option value="arms">Arms</option>
          <option value="legs">Legs</option>
        </select>
      </div>
      <div role="cell" className="rep-value">{metrics.reps}</div>
      <div role="cell" className="angle-value">{formatAngle(metrics.angle)}</div>
      <div role="cell" className="angle-value secondary-angle">{formatAngle(metrics.left)}</div>
      <div role="cell" className="angle-value secondary-angle">{formatAngle(metrics.right)}</div>
      <div role="cell">
        <span className="tracking-status">
          <span className="status-dot" aria-hidden="true" />
          {metrics.status}
        </span>
      </div>
      <div role="cell">
        <ReassignStudent student={student} students={students} />
      </div>
    </div>
  );
}

export function StudentList() {
  const [data, setData] = useState({ status: 'starting', students: [], error: null });

  useEffect(() => {
    let stopped = false;
    let timer;
    let controller;

    async function refresh() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch('/api/students', { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(`Server returned ${response.status}`);
        const snapshot = await response.json();
        if (!stopped) setData(snapshot);
      } catch (error) {
        if (!stopped) setData(current => ({
          ...current, status: 'error', error: 'Unable to get camera data. Check that Flask is running.',
          students: current.students.map(student => ({
            ...student, tracked: false,
            arms: { ...student.arms, angle: null, left: null, right: null, status: 'Lost tracking' },
            legs: { ...student.legs, angle: null, left: null, right: null, status: 'Lost tracking' },
          })),
        }));
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(refresh, 250);
      }
    }

    refresh();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
    };
  }, []);

  const message = data.error || (data.status === 'starting' ? 'Starting camera and pose model…' : 'No people detected.');
  function renameStudent(id, name) {
    setData(current => ({
      ...current,
      students: current.students.map(student => student.id === id ? { ...student, name } : student),
    }));
  }

  return (
    <div className="student-list" role="table" aria-label="Live student tracking" tabIndex={0}>
      <div className="header" role="row">
        {columns.map(label => <div key={label} role="columnheader">{label}</div>)}
      </div>
      <div className="students" role="rowgroup">
        {data.students.map(student => <StudentRow key={student.id} student={student} students={data.students} onRename={renameStudent} />)}
        {(data.students.length === 0 || data.error) && (
          <div role="row">
            <div role="cell" aria-colspan={8} className="detection-message">
              <span role="status">{message}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function App() {
  return (
    <div className="tracker-page">
      <TrackerTitle />
      <Divider />
      <TrackerDescription />
      <Divider />
      <PreviewToggle />
      <StudentList />
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
