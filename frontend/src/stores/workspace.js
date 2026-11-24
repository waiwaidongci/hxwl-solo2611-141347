import {request, saveFile} from '../api/http.js';
import {session} from './session.js';

const call = (path, body) => request(path, {token:session.token, body});
export const workspace = {
  spec: null,
  datasets: [],
  runs: [],
  async init() { this.spec = await request('/spec'); },
  async refresh() { [this.datasets, this.runs] = await Promise.all([call('/datasets'), call('/runs')]); },
  getDataset: (id, offset = 0) => call(`/datasets/${id}?offset=${offset}`),
  getRun: id => call(`/runs/${id}`),
  upload: data => call('/datasets', data),
  start: data => call('/runs', data),
  review: (id, data) => call(`/runs/${id}/review`, data),
  audit: () => call('/audit'),
  schedules: () => call('/schedules'),
  deliveries: () => call('/deliveries'),
  schedule: data => call('/schedules', data),
  disable: id => call(`/schedules/${id}/disable`, {}),
  revisions: datasetId => call(`/triage/revisions?dataset_id=${datasetId}`),
  registerRevision: data => call('/triage/revisions', data),
  recompute: data => call('/triage/recompute', data),
  triageLists: () => call('/triage/lists'),
  activeTriage: () => call('/triage/lists/active'),
  triageList: id => call(`/triage/lists/${id}`),
  dispatch: (id, data) => call(`/triage/lists/${id}/dispatch`, data),
  dispatches: listId => call(`/triage/dispatches${listId ? `?list_id=${listId}` : ''}`),
  download: (path, filename) => saveFile(path, session.token, filename),
};
