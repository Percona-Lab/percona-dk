/**
 * Percona DK usage tracker — Google Apps Script web app endpoint.
 *
 * Deploy: Extensions > Apps Script, paste this in, then
 *   Deploy > New deployment > Type: Web app
 *   Execute as: Me
 *   Who has access: Anyone
 * Copy the resulting /exec URL and give it to the sherpa cron.
 *
 * Expected POST body (JSON):
 *   {
 *     "secret": "<shared secret>",
 *     "date": "2026-04-28",
 *     "total_searches": 142,
 *     "peak_hour": 14,
 *     "peak_hour_count": 38,
 *     "distinct_queries": 89,
 *     "top_queries": [["wsrep error", 7], ["pmm install", 4], ...]
 *   }
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
    appendDailyUsage_(ss, body);
    appendTopQueries_(ss, body);

    return ContentService.createTextOutput("ok")
      .setMimeType(ContentService.MimeType.TEXT);
  } catch (err) {
    return ContentService.createTextOutput("error: " + err)
      .setMimeType(ContentService.MimeType.TEXT);
  }
}

function appendDailyUsage_(ss, body) {
  var sheet = ss.getSheetByName("Daily Usage");
  if (!sheet) {
    sheet = ss.insertSheet("Daily Usage");
    sheet.appendRow([
      "Date", "Total Searches", "Peak Hour (UTC)", "Peak Hour Count", "Distinct Queries"
    ]);
    sheet.getRange("A1:E1").setFontWeight("bold");
  }
  sheet.appendRow([
    body.date,
    body.total_searches,
    body.peak_hour,
    body.peak_hour_count,
    body.distinct_queries
  ]);
}

function appendTopQueries_(ss, body) {
  var sheet = ss.getSheetByName("Top Queries");
  if (!sheet) {
    sheet = ss.insertSheet("Top Queries");
    sheet.appendRow(["Date", "Query", "Count"]);
    sheet.getRange("A1:C1").setFontWeight("bold");
  }
  var rows = (body.top_queries || []).map(function (pair) {
    return [body.date, pair[0], pair[1]];
  });
  if (rows.length > 0) {
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, 3).setValues(rows);
  }
}
