for (const form of document.querySelectorAll('form[data-decision]')) {
  const controls = form.querySelector('fieldset');
  const boxes = Array.from(form.querySelectorAll('input'));
  const message = form.querySelector('[role=status]');
  let revision;

  function show(decision) {
    revision = decision.revision;
    for (const box of boxes) box.checked = decision.stages.includes(box.value);
    const required = boxes.filter(box => box.checked).map(box => box.labels[0].textContent.trim());
    const summary = required.length ? 'Required at: ' + required.join(', ') : 'Optional throughout';
    message.textContent = decision.decided_by ? summary + '. Decision by: ' + decision.decided_by + '.' : 'Undetermined';
    controls.disabled = false;
  }

  async function load() {
    const response = await fetch(form.dataset.decision);
    if (!response.ok) throw new Error();
    show(await response.json());
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    controls.disabled = true;
    try {
      const stages = boxes.filter(box => box.checked).map(box => box.value);
      const response = await fetch(form.dataset.decision, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({revision, stages})});
      if (response.status === 409) {
        await load();
        message.textContent += ' A newer decision was loaded. Review it before saving.';
      } else {
        if (!response.ok) throw new Error();
        show(await response.json());
      }
    } catch { message.textContent = 'Reopen the draft to check the saved decision.'; }
  });
  load().catch(() => { message.textContent = 'Could not load the stage decision. Reopen the draft.'; });
}
