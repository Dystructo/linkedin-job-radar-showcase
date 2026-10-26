/**
 * LinkedIn Job Radar: Google Sheet tracker web app.
 *
 * Paste this whole file into your Sheet (Extensions > Apps Script), then follow the README:
 *   1. Project Settings > Script properties > add SHARED_SECRET (a long random string).
 *   2. Run the setup() function once (it creates the header row and the status dropdown).
 *   3. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 *   4. Copy the web app URL into SHEET_WEBAPP_URL, and the secret into SHEET_SHARED_SECRET.
 *
 * "Anyone" can reach the URL, but every request must carry the shared secret, and the script can
 * only add rows (deduped by post_url) or read them. It never deletes or edits your rows.
 */

var SHEET_NAME = 'Tracker';
var HEADERS = ['date_found', 'relevance', 'role_category', 'role_title', 'company', 'location',
  'fresher_fit', 'poster_type', 'scam_risk', 'post_url', 'author', 'author_headline',
  'summary', 'why', 'status', 'notes'];
var STATUSES = ['applied', 'dm_sent', 'skip', 'not_relevant'];

function setup() {
  var sheet = getSheet_();
  sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
  sheet.setFrozenRows(1);
  var statusCol = HEADERS.indexOf('status') + 1;
  var rule = SpreadsheetApp.newDataValidation().requireValueInList(STATUSES, true)
    .setAllowInvalid(false).build();
  sheet.getRange(2, statusCol, sheet.getMaxRows() - 1, 1).setDataValidation(rule);
}

function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, error: 'bad JSON' });
  }
  var secret = PropertiesService.getScriptProperties().getProperty('SHARED_SECRET');
  if (!secret || body.secret !== secret) {
    return json_({ ok: false, error: 'unauthorized' });
  }
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    if (body.action === 'append') return json_(append_(body.rows || []));
    if (body.action === 'read') return json_({ ok: true, rows: read_() });
    return json_({ ok: false, error: 'unknown action' });
  } finally {
    lock.releaseLock();
  }
}

function append_(rows) {
  var sheet = getSheet_();
  if (sheet.getLastRow() === 0) setup();
  var seen = {};
  read_().forEach(function (r) { seen[r.post_url] = true; });
  var out = [];
  rows.forEach(function (row) {
    var url = String(row.post_url || '');
    if (!url || seen[url]) return;
    seen[url] = true;
    out.push(HEADERS.map(function (h) { return row[h] === undefined ? '' : row[h]; }));
  });
  if (out.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, out.length, HEADERS.length).setValues(out);
  }
  return { ok: true, added: out.length, skipped: rows.length - out.length };
}

function read_() {
  var sheet = getSheet_();
  var last = sheet.getLastRow();
  if (last < 2) return [];
  var values = sheet.getRange(2, 1, last - 1, HEADERS.length).getValues();
  return values.map(function (v) {
    var row = {};
    HEADERS.forEach(function (h, i) { row[h] = v[i]; });
    return row;
  });
}

function getSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
