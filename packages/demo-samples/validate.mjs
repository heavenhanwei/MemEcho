import { readFileSync, readdirSync, statSync } from "fs";
import { join, resolve } from "path";

const SAMPLES_DIR = resolve(import.meta.dirname, "samples");

const REQUIRED_RESULT_FIELDS = [
  "schema_version",
  "request_id",
  "analysis_mode",
  "scope",
  "minutes",
  "content_analysis",
  "participants",
  "vad_series",
  "interaction_events",
  "self_echo",
  "coaching",
  "insights",
  "evidence",
  "uncertainties",
  "provenance",
  "memory",
];

const REQUIRED_SCOPE_FIELDS = [
  "single_session",
  "signals_used",
  "signals_missing",
  "quality",
  "target_participant_ids",
  "self_participant_id",
  "self_identity_basis",
];

const REQUIRED_MINUTES_FIELDS = [
  "summary",
  "focus",
  "consensus",
  "disagreements",
  "explicit_actions",
  "recommendations",
];

let errors = 0;

function fail(sample, msg) {
  console.error(`  FAIL [${sample}]: ${msg}`);
  errors += 1;
}

function validateSample(dir) {
  const name = dir.replace(SAMPLES_DIR + "\\", "").replace(SAMPLES_DIR + "/", "");
  console.log(`\nValidating: ${name}`);

  // Check metadata.json
  const metaPath = join(dir, "metadata.json");
  try {
    const meta = JSON.parse(readFileSync(metaPath, "utf-8"));
    if (!meta.id) fail(name, "metadata.json missing 'id'");
    if (!meta.title) fail(name, "metadata.json missing 'title'");
    if (!meta.source_mode) fail(name, "metadata.json missing 'source_mode'");
    console.log("  metadata.json OK");
  } catch (e) {
    fail(name, `metadata.json: ${e.message}`);
  }

  // Check report.json
  const reportPath = join(dir, "report.json");
  try {
    const report = JSON.parse(readFileSync(reportPath, "utf-8"));

    for (const field of REQUIRED_RESULT_FIELDS) {
      if (!(field in report)) fail(name, `report.json missing '${field}'`);
    }

    if (report.schema_version !== "1.1")
      fail(name, `schema_version should be "1.1", got "${report.schema_version}"`);

    // Scope
    for (const field of REQUIRED_SCOPE_FIELDS) {
      if (!(field in report.scope)) fail(name, `scope missing '${field}'`);
    }

    // Minutes
    for (const field of REQUIRED_MINUTES_FIELDS) {
      if (!(field in report.minutes)) fail(name, `minutes missing '${field}'`);
    }

    // Evidence refs integrity
    const evidenceIds = new Set(report.evidence.map((e) => e.id));
    for (const insight of report.insights) {
      for (const ref of insight.evidence_refs) {
        if (!evidenceIds.has(ref))
          fail(name, `insight ${insight.id} references missing evidence '${ref}'`);
      }
    }

    // Participants referenced in content_analysis exist
    const participantIds = new Set(report.participants.map((p) => p.id));
    for (const ca of report.content_analysis) {
      if (!participantIds.has(ca.participant_id))
        fail(name, `content_analysis references missing participant '${ca.participant_id}'`);
    }

    console.log(`  report.json OK (${report.evidence.length} evidence, ${report.insights.length} insights, ${report.participants.length} participants)`);
  } catch (e) {
    fail(name, `report.json: ${e.message}`);
  }

  // Check report.md exists
  try {
    const md = readFileSync(join(dir, "report.md"), "utf-8");
    if (!md.includes("# memEcho")) fail(name, "report.md missing expected header");
    console.log("  report.md OK");
  } catch (e) {
    fail(name, `report.md: ${e.message}`);
  }

  // Check report.html exists
  try {
    const html = readFileSync(join(dir, "report.html"), "utf-8");
    if (!html.includes("<!DOCTYPE html>")) fail(name, "report.html missing DOCTYPE");
    console.log("  report.html OK");
  } catch (e) {
    fail(name, `report.html: ${e.message}`);
  }
}

// Run
console.log("memEcho Demo Samples Validator\n");
const samples = readdirSync(SAMPLES_DIR).filter((entry) =>
  statSync(join(SAMPLES_DIR, entry)).isDirectory()
);

if (samples.length === 0) {
  console.error("No sample directories found!");
  process.exit(1);
}

for (const sample of samples) {
  validateSample(join(SAMPLES_DIR, sample));
}

console.log(`\n${"=".repeat(40)}`);
if (errors > 0) {
  console.error(`\n${errors} error(s) found.`);
  process.exit(1);
} else {
  console.log(`\nAll ${samples.length} sample(s) validated successfully.`);
}
