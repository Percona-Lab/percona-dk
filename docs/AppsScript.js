/**
 * Percona DK Install Analytics - Google Apps Script
 *
 * Deploy as Web App: Execute as Me, Access: Anyone.
 * Logs successful installs to a Google Sheet for tracking adoption.
 *
 * Sheet setup: Create a sheet named "Installs" with headers:
 *   Timestamp | MachineHash | Version | OS | Platform | RepoCount | Stacks
 *
 * Privacy: Only receives a SHA-256 hash of the machine ID.
 * The raw hardware UUID never leaves the user's machine.
 */

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);

    if (data.action === "install") {
      return handleInstall(data);
    }

    return jsonResponse({ status: "error", message: "Unknown action" });
  } catch (err) {
    return jsonResponse({ status: "error", message: err.toString() });
  }
}

function handleInstall(data) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Installs");

  if (!sheet) {
    sheet = ss.insertSheet("Installs");
    sheet.appendRow([
      "Timestamp", "MachineHash", "Version", "OS", "Platform", "RepoCount", "Stacks"
    ]);
  }

  var machineHash = (data.machine_hash || "unknown").substring(0, 16);
  var version = data.app_version || "unknown";

  // Dedup: same machine + same version = don't log again
  var existing = sheet.getDataRange().getValues();
  for (var i = 1; i < existing.length; i++) {
    if (existing[i][1] === machineHash && existing[i][2] === version) {
      return jsonResponse({ status: "ok", message: "Already logged" });
    }
  }

  sheet.appendRow([
    new Date().toISOString(),
    machineHash,
    version,
    data.os_version || "",
    data.platform || "",
    data.repo_count || 0,
    data.stacks || "",
  ]);

  return jsonResponse({ status: "ok", message: "Install logged" });
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
