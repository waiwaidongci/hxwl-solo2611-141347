import {datasetField, feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {TRIAGE_STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const OUTCOMES = ['完好', '结构修理', '门封修理', '烘干处理', '清洗处理', '隔离处置']
  .map(item => [item, item]);

function revisionForm(datasets, refresh, message) {
  const submit = button('登记实判更正', {class:'primary', type:'submit', 'data-testid':'revision-submit'});
  return el('form', {class:'fields', onSubmit:handler(submit, message, async event => {
    const data = new FormData(event.target);
    await workspace.registerRevision({
      dataset_id:Number(data.get('dataset_id')), ticket_id:data.get('ticket_id').trim(),
      revision:Number(data.get('revision')), posted_at:data.get('posted_at'),
      audit_outcome:data.get('audit_outcome'),
    });
    event.target.reset();
    await refresh();
  })}, datasetField(datasets),
    field({key:'ticket_id', label:'设备交接单号', type:'string', max_length:48}),
    field({key:'revision', label:'修订号（须大于已登记修订号）', type:'int', min:1, max:32767}),
    field({key:'posted_at', label:'实判实际入账日', type:'date'}),
    field({key:'audit_outcome', label:'更正后实判结论', type:'select', required:true, options:OUTCOMES}),
    el('p', {class:'subtle'}, '新旧修订允许乱序到货：系统按修订号判定新旧，按入账日判定各截止点可见性。'),
    submit, message);
}

function recomputeForm(datasets, spec, refresh) {
  const message = feedback();
  const submit = button('受理重算', {class:'primary', type:'submit', 'data-testid':'recompute-submit'});
  return el('form', {class:'fields', onSubmit:handler(submit, message, async event => {
    const data = new FormData(event.target);
    const item = await workspace.recompute({
      dataset_id:Number(data.get('dataset_id')), name:`当班分流重算 ${new Date().toLocaleString('zh-CN')}`,
      params:Object.fromEntries(spec.params.map(param => [param.key, data.get(param.key)])),
    });
    await refresh();
    poll(() => workspace.triageList(item.id), row => ['active','superseded','failed'].includes(row.status),
      () => true).then(() => refresh());
  })}, datasetField(datasets),
    el('div', {class:'form-grid'}, spec.params.map(param => field(param))),
    el('p', {class:'subtle'}, '重算期间现场继续沿用旧清单；整份算完后一次性切换。较早受理的任务即使晚完成也不会覆盖较新清单。'),
    submit, message);
}

function dispatchPanel(active, refresh, message) {
  if (!active) return panel('当班派单', empty('当前没有生效清单，请先受理重算。'));
  const rows = active.result.snapshot || [];
  const selectAll = el('input', {type:'checkbox', 'aria-label':'全选本清单交接单'});
  const checkboxes = new Map();
  const send = button('派单给收件团队', {class:'primary', type:'button', 'data-testid':'dispatch-submit'});
  const recipient = field({key:'recipient', label:'收件团队', type:'select', required:false,
    options:workspace.spec.recipients.map(item => [item, item])});
  const recipientSelect = recipient.querySelector('select');
  send.addEventListener('click', handler(send, message, async () => {
    const ids = [...checkboxes.values()].filter(input => input.checked).map(input => input.value);
    if (!ids.length) throw new Error('请至少勾选一张交接单');
    const team = recipientSelect.value;
    const result = await workspace.dispatch(active.id, {ticket_ids:ids, recipient:team});
    message.textContent = result.created.length
      ? `已派单 ${result.created.length} 张，跳过已派单 ${result.skipped_ticket_ids.length} 张（重试不重复派单）。`
      : '所选交接单此前均已派单，未产生重复派单。';
    await refresh();
  }));
  const listTable = table([
    {label:'选择', render:row => {
      const input = el('input', {type:'checkbox', value:row.ticket_id,
        disabled:row.frozen, 'aria-label':`选择 ${row.ticket_id}`});
      if (row.frozen) input.title = '已派单，保留当时实判依据';
      checkboxes.set(row.ticket_id, input);
      return input;
    }},
    {key:'ticket_id', label:'交接单'}, {key:'lane', label:'通道'},
    {label:'异常票（%）', render:row => value(row.risk)},
    {label:'修订号', render:row => value(row.revision_used)},
    {label:'依据', render:row => row.frozen ? pill('派单快照保留', 'frozen') : pill('本次重算', 'fresh')},
    {label:'改道提示', render:row => row.changed ? pill('若重算将改道，维持原派单', 'warn') : '—'},
  ], rows, {caption:'生效清单（勾选后派单）', pageSize:10});
  selectAll.addEventListener('change', () => {
    for (const input of checkboxes.values()) if (!input.disabled) input.checked = selectAll.checked;
  });
  return panel('当班派单', p(`清单 #${active.seq} · ${date(active.activated_at)} 生效`, 'subtle'),
    el('div', {class:'actions'}, selectAll, p('全选未派单单', 'subtle'), recipient, send, message), listTable);
}

export async function triagePage(route, refresh) {
  const {spec, datasets} = workspace;
  const message = feedback();
  let active = null;
  let lists = [];
  let revisions = [];
  let dispatches = [];
  try {
    [lists, revisions, dispatches] = await Promise.all([
      workspace.triageLists(), workspace.revisions(datasets[0]?.id || route.id || 1),
      workspace.dispatches()]);
    active = await workspace.activeTriage().catch(() => null);
  } catch (error) {
    return panel(spec.pages[6] || '当班分流重算', el('div', {class:'empty', role:'alert'}, p(error.message)));
  }

  // 清单详情模式：展示指定版本的完整重算结果。
  if (route.id) {
    const detail = await workspace.triageList(route.id);
    if (!detail.result) {
      return panel(`分流清单 #${detail.seq}`, pill(TRIAGE_STATUS[detail.status], detail.status),
        p(detail.error || '该清单尚未生成结果。', 'feedback'), link('返回当班分流重算', '#triage', {class:'quiet'}));
    }
    return el('div', {},
      panel(`分流清单 #${detail.seq} · ${TRIAGE_STATUS[detail.status]}`,
        p(detail.result.summary, 'intro-copy'),
        el('div', {class:'actions'}, pill(TRIAGE_STATUS[detail.status], detail.status),
          link('返回当班分流重算', '#triage', {class:'quiet'})),
        metrics(detail.result.metrics)),
      ...detail.result.tables.map(item =>
        panel(item.title, table(item.columns, item.rows, {caption:item.title, pageSize:10}))));
  }

  const busy = lists.some(item => ['queued', 'running'].includes(item.status));
  if (busy) {
    poll(() => workspace.triageLists(), rows => rows.every(item =>
      !['queued', 'running'].includes(item.status)), () => true).then(() => refresh());
  }
  const listPanel = panel('重算受理记录', table([
    {label:'序号', render:row => `#${row.seq}`},
    {label:'状态', render:row => pill(TRIAGE_STATUS[row.status], row.status)},
    {label:'纳入修订', render:row => row.revision_count},
    {label:'受理时间', render:row => date(row.created_at)},
    {label:'生效时间', render:row => date(row.activated_at)},
    {label:'操作', render:row => link('查看清单', `#triage/${row.id}`)},
    {key:'error', label:'备注'},
  ], lists, {caption:'重算受理记录', empty:'还未受理过重算。'}));

  const revisionPanel = panel('实判更正流水', table([
    {key:'ticket_id', label:'交接单'}, {key:'revision', label:'修订号'},
    {key:'posted_at', label:'入账日'}, {key:'audit_outcome', label:'更正后实判'},
    {key:'author', label:'登记人'}, {label:'登记时间', render:row => date(row.created_at)},
  ], revisions, {caption:'实判修订（乱序到货按修订号判新旧）', empty:'暂无更正记录。'}));

  const dispatchLog = panel('派单回执', table([
    {key:'ticket_id', label:'交接单'}, {key:'lane', label:'派单通道'}, {key:'recipient', label:'收件团队'},
    {label:'采用修订号', render:row => value(row.evidence_snapshot?.revision)},
    {key:'created_by', label:'派单人'}, {label:'派单时间', render:row => date(row.created_at)},
  ], dispatches, {caption:'派单回执（重试不重复派单）', empty:'尚未派单。'}));

  const forms = session.canWrite
    ? (datasets.length
        ? el('div', {class:'two-col'},
            panel('登记实判更正', revisionForm(datasets, refresh, feedback())),
            panel('重算当班分流清单', recomputeForm(datasets, spec, refresh)))
        : empty('请先导入数据批次。', '#inputs', '前往导入'))
    : p('只读角色可查看修订、清单版本与派单回执。', 'subtle');

  return el('div', {},
    panel(spec.pages[6] || '当班分流重算',
      p('实判更正带修订号且可能乱序到货。训练、验证、留出按初检日归段，每段只取截止前实际入账的最高修订；已派单保留当时实判依据。', 'intro-copy'),
      busy && el('p', {role:'status', class:'feedback'}, '后台正在重算，当前生效清单保持不变，完成后自动切换。')),
    forms,
    dispatchPanel(active, refresh, message),
    el('div', {class:'two-col'}, listPanel, revisionPanel),
    dispatchLog,
    active && panel('重算版清单明细', ...active.result.tables.map(item =>
      panel(item.title, table(item.columns, item.rows, {caption:item.title, pageSize:10})))));
}
