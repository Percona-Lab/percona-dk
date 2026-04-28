/**
 * Percona DK usage tracker — Google Apps Script web app endpoint.
 *
 * Deploy: Extensions > Apps Script, paste this in, then
 *   Deploy > New deployment > Type: Web app
 *   Execute as: Me
 *   Who has access: Anyone
 * After any code change: Deploy > Manage deployments > edit > Version: New version.
 *
 * Upserts by date: re-running for a date that already exists replaces the
 * existing rows rather than appending duplicates.
 */

// CHANGE THIS to a long random string. Must match WEBHOOK_SECRET on sherpa.
var SHARED_SECRET = "CHANGE_ME_TO_A_LONG_RANDOM_STRING";

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.secret !== SHARED_SECRET) {
      return ContentService.createTextOutput("forbidden")
        .setMimeType(ContentService.MimeType.TEXT);
    }

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    upsertDailyUsage_(ss, body);
    upsertTopQueries_(ss, body);

    return ContentService.createTextOutput("ok")
      .setMimeType(ContentService.MimeType.TEXT);
  } catch (err) {
    return ContentService.createTextOutput("error: " + err)
      .setMimeType(ContentService.MimeType.TEXT);
  }
}

function upsertDailyUsage_(ss, body) {
  var sheet = ss.getSheetByName("Daily Usage");
  if (!sheet) {
    sheet = ss.insertSheet("Daily Usage");
    sheet.appendRow([
      "Date", "Total Searches", "Peak Hour (UTC)", "Peak Hour Count", "Distinct Queries"
    ]);
    sheet.getRange("A1:E1").setFontWeight("bold");
  }
  var row = [
    body.date,
    body.total_searches,
    body.peak_hour,
    body.peak_hour_count,
    body.distinct_queries
  ];
  var existingRow = findRowByDate_(sheet, body.date);
  if (existingRow > 0) {
    sheet.getRange(existingRow, 1, 1, row.length).setValues([row]);
  } else {
    sheet.appendRow(row);
  }
}

function upsertTopQueries_(ss, body) {
  var sheet = ss.getSheetByName("Top Queries");
  if (!sheet) {
    sheet = ss.insertSheet("Top Queries");
    sheet.appendRow(["Date", "Query", "Count"]);
    sheet.getRange("A1:C1").setFontWeight("bold");
  }
  deleteRowsByDate_(sheet, body.date);
  var rows = (body.top_queries || []).map(function (pair) {
    return [body.date, pair[0], pair[1]];
  });
  if (rows.length > 0) {
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, 3).setValues(rows);
  }
}

// Returns the 1-based row number whose column A matches the given date,
// or 0 if not found. Skips header row.
function findRowByDate_(sheet, date) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return 0;
  var values = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  for (var i = 0; i < values.length; i++) {
    if (formatDateCell_(values[i][0]) === date) {
      return i + 2;
    }
  }
  return 0;
}

// Deletes every data row whose column A matches the given date.
// Iterates bottom-up so row indices stay valid.
function deleteRowsByDate_(sheet, date) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
  var values = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  for (var i = values.length - 1; i >= 0; i--) {
    if (formatDateCell_(values[i][0]) === date) {
      sheet.deleteRow(i + 2);
    }
  }
}

// Cell values come back as either Date objects (if Sheets auto-parsed the
// ISO date) or strings. Normalize to "YYYY-MM-DD" for comparison.
function formatDateCell_(v) {
  if (v instanceof Date) {
    return Utilities.formatDate(v, "UTC", "yyyy-MM-dd");
  }
  return String(v);
}
