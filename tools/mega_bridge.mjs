// Private MEGA bridge, started by app/mega_service.py. Not a public entry point.
// Credentials come only from the MEGA_EMAIL / MEGA_PASSWORD environment variables (never argv).
// stdout carries only the result (JSON for upload, raw bytes for download).
// On failure the only thing written to stderr is {"error": "<code>"}; messages from the library
// are never printed, so credentials and node ids cannot leak into logs.
import fs from "node:fs";
import { pipeline } from "node:stream/promises";
import { Storage } from "megajs";

function fail(code) {
  process.stderr.write(JSON.stringify({ error: code }) + "\n");
  process.exit(1);
}

function classify(error) {
  const text = String((error && (error.code || error.message)) || "").toLowerCase();
  if (/econn|etimedout|enotfound|eai_again|network|socket|timeout|-3\b/.test(text)) return "network";
  if (/credential|password|login|session|blocked|-15\b|-16\b/.test(text)) return "auth";
  if (/not found|enoent|-9\b/.test(text)) return "not_found";
  return "failed";
}

async function connect() {
  const email = process.env.MEGA_EMAIL || "";
  const password = process.env.MEGA_PASSWORD || "";
  if (!email || !password) fail("not_configured");
  try {
    return await new Storage({ email, password, userAgent: "DigicraftAcademy/1.0" }).ready;
  } catch (error) {
    fail(classify(error) === "network" ? "network" : "auth");
  }
}

async function ensureFolder(parent, name) {
  const existing = (parent.children || []).find((child) => child.directory && child.name === name);
  return existing || (await parent.mkdir(name));
}

async function upload(filePath, size, name, folders) {
  const storage = await connect();
  try {
    let folder = storage.root;
    for (const part of folders) folder = await ensureFolder(folder, part);
    const source = fs.createReadStream(filePath);
    const target = folder.upload({ name, size: Number(size) }, source);
    const file = await target.complete;
    process.stdout.write(JSON.stringify({ nodeId: file.nodeId }) + "\n");
  } catch (error) {
    fail(classify(error));
  } finally {
    try { await storage.close(); } catch { /* ignore */ }
  }
}

async function download(nodeId) {
  const storage = await connect();
  try {
    const file = storage.files && storage.files[nodeId];
    if (!file || file.directory) fail("not_found");
    await pipeline(file.download(), process.stdout);
  } catch (error) {
    fail(classify(error));
  } finally {
    try { await storage.close(); } catch { /* ignore */ }
  }
}

const [command, ...args] = process.argv.slice(2);
if (command === "upload" && args.length >= 4) {
  const [filePath, size, name, ...folders] = args;
  await upload(filePath, size, name, folders);
} else if (command === "download" && args.length === 1) {
  await download(args[0]);
} else {
  fail("failed");
}
