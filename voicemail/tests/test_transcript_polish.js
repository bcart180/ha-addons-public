#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const appJs = fs.readFileSync(path.join(root, "rootfs/app/static/app.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(root, "rootfs/app/static/index.html"), "utf8");
const config = fs.readFileSync(path.join(root, "config.yaml"), "utf8");
const changelog = fs.readFileSync(path.join(root, "CHANGELOG.md"), "utf8");
const server = fs.readFileSync(path.join(root, "rootfs/app/server.py"), "utf8");

function extractFunction(name) {
  const re = new RegExp("function " + name + "\\s*\\([^)]*\\)\\s*\\{");
  const m = re.exec(appJs);
  if (!m) throw new Error("missing function " + name);
  let i = m.index + m[0].length;
  let depth = 1;
  while (i < appJs.length && depth > 0) {
    const c = appJs[i];
    if (c === "'" || c === '"' || c === "`") {
      const q = c;
      i++;
      while (i < appJs.length && appJs[i] !== q) {
        if (appJs[i] === "\\") i++;
        i++;
      }
    } else if (c === "{") depth++;
    else if (c === "}") depth--;
    i++;
  }
  return appJs.slice(m.index, i);
}

const api = new Function(
  extractFunction("mapSpeakerLine") +
    "\n" +
    extractFunction("mapSpeakerText") +
    "\n" +
    extractFunction("spokenDuration") +
    "\n" +
    extractFunction("polishTranscript") +
    "\nreturn { spokenDuration, polishTranscript, mapSpeakerLine, mapSpeakerText };"
)();

const failures = [];
function eq(actual, expected, label) {
  if (actual !== expected) failures.push(`${label}\n  got: ${JSON.stringify(actual)}\n  want: ${JSON.stringify(expected)}`);
}

eq(api.spokenDuration("10.77"), "10 seconds", "10.77 floors to 10 seconds");
eq(api.spokenDuration("10"), "10 seconds", "10 seconds");
eq(api.spokenDuration("1"), "1 second", "1 second");
eq(api.spokenDuration("0.9"), "0 seconds", "0.9 floors to 0 seconds");
eq(api.spokenDuration("0"), "0 seconds", "0 seconds");
eq(api.spokenDuration("60"), "1 minute", "1 minute");
eq(api.spokenDuration("61"), "1 minute 1 second", "1 minute 1 second");
eq(api.spokenDuration("65.9"), "1 minute 5 seconds", "65.9 is 1 minute 5 seconds");
eq(api.spokenDuration("120"), "2 minutes", "2 minutes");
eq(api.spokenDuration("121"), "2 minutes 1 second", "2 minutes 1 second");
eq(api.spokenDuration("122.2"), "2 minutes 2 seconds", "2 minutes 2 seconds");
eq(api.spokenDuration(".."), null, "invalid number");

const sample = [
  "Source: twilio_house_voice",
  "Duration_s: 10.77",
  "TRANSCRIPT",
  "Please call (555) 555-0100 to cancel.",
].join("\n");
eq(
  api.polishTranscript(sample),
  ["Duration: 10 seconds", "Transcript", "Please call (555) 555-0100 to cancel."].join("\n"),
  "hangup header block"
);
eq(api.polishTranscript("source: TWILIO_HOUSE_VOICE"), "", "source case: line dropped");
eq(api.polishTranscript("Source: CallCatch\nHello"), "Hello", "branded source line dropped");
if (/Source:\s*CallCatch/.test(api.polishTranscript(sample))) failures.push("no 'Source: CallCatch' in polished output");
eq(api.polishTranscript("duration_s: 60"), "Duration: 1 minute", "duration case");
eq(api.polishTranscript("Duration_s:65"), "Duration: 1 minute 5 seconds", "duration without space");
eq(api.polishTranscript("transcript:"), "Transcript", "transcript header with colon");
eq(api.polishTranscript("Source: other_bridge"), "Source: other_bridge", "other source untouched");
eq(api.polishTranscript("Duration: 10 seconds"), "Duration: 10 seconds", "already friendly");
eq(
  api.polishTranscript("The line mentions Source: twilio_house_voice mid-text"),
  "The line mentions Source: twilio_house_voice mid-text",
  "not a full line"
);
eq(api.polishTranscript("Duration_s: 10.77 extra"), "Duration_s: 10.77 extra", "trailing junk");
eq(
  api.polishTranscript("Source: twilio_house_voice\r\nDuration_s: 1\r\n"),
  "Duration: 1 second\n",
  "crlf and trailing newline"
);

const greeting = "House: Hi, you've reached the house. This is the assistant. How may I help you?";
eq(
  api.mapSpeakerLine(greeting),
  "Assistant: Hi, you've reached the house. This is the assistant. How may I help you?",
  "House greeting keeps the spoken word assistant"
);
eq(api.mapSpeakerLine("house: hello"), "Assistant: hello", "house");
eq(api.mapSpeakerLine("HOUSE: hello"), "Assistant: hello", "HOUSE");
eq(api.mapSpeakerLine("assistant: hello"), "Assistant: hello", "assistant");
eq(api.mapSpeakerLine("Assistant: hello"), "Assistant: hello", "Assistant already");
eq(api.mapSpeakerLine("agent:hello"), "Assistant: hello", "agent without space");
eq(api.mapSpeakerLine("Agent: hello"), "Assistant: hello", "Agent");
eq(api.mapSpeakerLine("caller: Hi this is Sam"), "Caller: Hi this is Sam", "caller");
eq(api.mapSpeakerLine("Caller: Hi"), "Caller: Hi", "Caller already");
eq(api.mapSpeakerLine("CALLER: Hi"), "Caller: Hi", "CALLER");
eq(api.mapSpeakerLine("House:"), "Assistant:", "label only");
eq(api.mapSpeakerLine("  House: hi"), "  Assistant: hi", "keeps indent");
eq(api.mapSpeakerLine("Household: stays"), "Household: stays", "Household is not a role");
eq(
  api.mapSpeakerLine("This is his assistant. How may I help you?"),
  "This is his assistant. How may I help you?",
  "assistant inside the sentence"
);
eq(api.mapSpeakerLine("Clinic: Appointment"), "Clinic: Appointment", "other colon label");
eq(
  api.mapSpeakerText("caller: Hi\r\nHouse: Hello from the house\r\n"),
  "Caller: Hi\nAssistant: Hello from the house\n",
  "multiline roles"
);
eq(
  api.polishTranscript(
    ["Source: twilio_house_voice", "Duration_s: 10.77", "TRANSCRIPT", "House: Please call 208-555-0100.", "caller: Thanks"].join("\n")
  ),
  ["Duration: 10 seconds", "Transcript", "Assistant: Please call 208-555-0100.", "Caller: Thanks"].join("\n"),
  "headers and speakers together"
);
eq(
  api.polishTranscript("The line mentions House: mid-text"),
  "The line mentions House: mid-text",
  "House mid-line is not a speaker"
);

const detail = appJs.indexOf('linkPhones(polishTranscript(v.transcript || v.snippet || "No transcript"))');
if (detail < 0) failures.push("detail transcript does not polish before linkPhones");
if (!appJs.includes("mapSpeakerText(v.snippet")) failures.push("list preview does not map speaker labels");
const share = appJs.slice(appJs.indexOf('id="btn-share"') === -1 ? appJs.indexOf("btn-share") : 0);
if (
  !share.includes("current.transcript || current.snippet") ||
  share.includes("polishTranscript(current") ||
  share.includes("mapSpeaker")
) {
  failures.push("share sheet should keep the stored transcript");
}
if (!/^slug:\s*voicemail\s*$/m.test(config)) failures.push("slug must stay voicemail");
if (!/^version:\s*"0\.4\.1"\s*$/m.test(config)) failures.push("config version must be 0.4.1");
if (!changelog.startsWith("# Changelog\n\n## 0.4.1 — 2026-11-01\n")) failures.push("changelog missing 0.4.1 entry");
if (!/## 0\.1\.9 — 2026-10-29\n/.test(changelog)) failures.push("changelog must keep the 0.1.9 entry");
if (!/## 0\.1\.8 — 2026-10-29\n/.test(changelog)) failures.push("changelog must keep the 0.1.8 packaging entry");
if (!/## 0\.1\.7 — 2026-10-28\n/.test(changelog)) failures.push("changelog must keep the 0.1.7 entry");
if (!/VERSION = "0\.4\.1"/.test(server)) failures.push("server.py VERSION must match 0.4.1");
if (/^image:/m.test(config)) failures.push("config must not set an image field");
if (!/^ingress_panel:\s*true\s*$/m.test(config)) failures.push("ingress_panel must stay true");
if (/bedroom_homepod|HomePod/.test([server, config, appJs, indexHtml].join("\n"))) {
  failures.push("no HomePod fallback");
}
for (const key of ["startup:", "boot:", "ingress_port:", "watchdog:", "hassio_role:", "backup:"]) {
  if (new RegExp("^" + key, "m").test(config)) failures.push("config must not set " + key);
}
if (appJs.includes("soft-slate")) failures.push("app.js should not touch Soft Slate CSS");

if (failures.length) {
  console.error(failures.join("\n\n"));
  process.exit(1);
}
console.log("ok transcript polish");
