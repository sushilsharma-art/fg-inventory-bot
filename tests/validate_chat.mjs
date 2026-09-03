import fs from "node:fs";
import vm from "node:vm";
import crypto from "node:crypto";
import zlib from "node:zlib";

const root = new URL("../", import.meta.url);
const html = fs.readFileSync(new URL("site/index.html", root), "utf8");
const envelope = JSON.parse(fs.readFileSync(new URL("site/data.enc.json", root), "utf8"));
const passcode = fs.readFileSync("C:/Users/itsup/Desktop/DS FR/whatsapp/passcode.txt", "utf8").trim();

function decrypt(value) {
  const salt = Buffer.from(value.salt, "base64");
  const iv = Buffer.from(value.iv, "base64");
  const combined = Buffer.from(value.ct, "base64");
  const ciphertext = combined.subarray(0, -16);
  const authTag = combined.subarray(-16);
  const key = crypto.pbkdf2Sync(passcode, salt, Number(value.iter), 32, "sha256");
  const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAuthTag(authTag);
  let plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  if (value.zip === "gzip") plaintext = zlib.gunzipSync(plaintext);
  return JSON.parse(plaintext.toString("utf8"));
}

class ElementMock {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.hidden = true;
    this.style = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this._html = "";
    this._bubble = null;
  }
  set innerHTML(value) {
    this._html = String(value);
    if (this._html.includes('class="bub"')) {
      this._bubble = new ElementMock("bubble");
      this._bubble._html = this._html;
    }
  }
  get innerHTML() { return this._html; }
  appendChild(child) { this.children.push(child); this.scrollHeight = this.children.length; return child; }
  addEventListener() {}
  focus() {}
  querySelector(selector) { return selector === ".bub" ? this._bubble : null; }
  querySelectorAll() { return []; }
  closest() { return this; }
  remove() {}
}

const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, new ElementMock(id));
  return elements.get(id);
};

globalThis.window = globalThis;
globalThis.document = {
  getElementById: element,
  createElement: tag => new ElementMock(tag),
  body: new ElementMock("body"),
  execCommand: () => true,
};
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
globalThis.localStorage = {
  values: new Map(),
  getItem(key) { return this.values.get(key) ?? null; },
  setItem(key, value) { this.values.set(key, String(value)); },
};
globalThis.setTimeout = fn => { fn(); return 1; };
globalThis.clearTimeout = () => {};
globalThis.open = () => {};
globalThis.FG_DATA = decrypt(envelope);

const start = html.indexOf("window.__START=function(){");
const marker = "\n};\n</script>\n<script>/* html2canvas";
const end = html.indexOf(marker, start);
if (start < 0 || end < 0) throw new Error("Could not isolate the chat engine.");
vm.runInThisContext(html.slice(start, end + 3), { filename: "chat-engine.js" });
window.__START();

function ask(query) {
  window.__ask(query);
  const rows = element("chat").children;
  const last = rows[rows.length - 1];
  return last?._bubble?.innerHTML ?? "";
}

function assertIncludes(value, expected, label) {
  if (!value.includes(expected)) throw new Error(`${label}: expected ${JSON.stringify(expected)} in ${value.slice(0, 400)}`);
}

assertIncludes(ask("summary"), "FG snapshot", "summary command");
assertIncludes(ask("help"), "Ask naturally", "help command");

const mumbaiSku = FG_DATA.skus.find(item => item.locs.some(location => location.n === "Mumbai"));
if (!mumbaiSku) throw new Error("No Mumbai SKU is available for context testing.");
assertIncludes(ask(`how much stock do we have for ${mumbaiSku.code}`), mumbaiSku.code, "natural SKU question");
assertIncludes(ask("this sku in Mumbai"), "Mumbai", "contextual location follow-up");

const b2bMumbaiSku = FG_DATA.skus.find(item => item.locs.some(location => location.n === "B2B Mumbai"));
if (!b2bMumbaiSku) throw new Error("No separate B2B Mumbai inventory location is available.");
assertIncludes(
  ask(`${b2bMumbaiSku.code} in B2B Mumbai`),
  "B2B Mumbai",
  "B2B Mumbai location question",
);

const shelfLife = ask("near expiry");
if (!shelfLife.includes("Shelf-life attention") && !shelfLife.includes("No ≤80%")) {
  throw new Error("near-expiry command did not return a valid response");
}

const zeroStock = ask("out of stock");
if (!zeroStock.includes("zero SOH") && !zeroStock.includes("No selling SKU")) {
  throw new Error("out-of-stock command did not return a valid response");
}

assertIncludes(ask("secondary sales"), "Secondary sales", "secondary sales summary");
assertIncludes(ask("what is through date"), "latest actual order date", "through date definition");
assertIncludes(ask("channel drr"), "Secondary sales", "channel DRR summary");
const channelLevelSales = ask("channel level sale");
assertIncludes(channelLevelSales, "Channel level sales", "channel-level sales command");
assertIncludes(channelLevelSales, "Last 3M", "channel-level three-month sales");
assertIncludes(channelLevelSales, "Total Sales", "channel-level total sales");
if (FG_DATA.secondarySales.monthlyHistory?.months?.length) {
  assertIncludes(ask("last 2 months sale"), "Monthly secondary sales", "rolling two-month history");
  assertIncludes(ask("July sales"), "Jul 2026", "named monthly history");
}
const firstChannel = FG_DATA.secondarySales.channels[0];
const specificChannelSales = ask(`${firstChannel.name} sales`);
assertIncludes(specificChannelSales, firstChannel.name, "specific channel sales");
assertIncludes(specificChannelSales, "Last 3M", "specific channel three-month sales");
const secondarySku = FG_DATA.skus.find(item => item.secDRR > 0);
if (!secondarySku) throw new Error("No SKU has Secondary DRR.");
assertIncludes(ask(`${secondarySku.code} channel drr`), "channel DRR split", "SKU channel DRR");
const secondaryCard = ask(secondarySku.code);
assertIncludes(secondaryCard, "Secondary · Overall", "SKU secondary DOI card");
assertIncludes(secondaryCard, "Secondary · Mumbai", "SKU Secondary Mumbai DOI card");
assertIncludes(secondaryCard, "WEB", "WEB display row");
if (secondaryCard.includes("<td>3PL</td>")) throw new Error("Legacy 3PL display label is still present.");
if (secondaryCard.indexOf("Secondary · Overall") > secondaryCard.indexOf("Primary · Overall")) {
  throw new Error("Secondary Overall must appear before Primary Overall.");
}
const secondaryMumbaiSku = FG_DATA.skus.find(item => item.secMumbaiDRR > 0 && item.locs.some(location => location.n === "Mumbai" && location.s > 0));
if (!secondaryMumbaiSku) throw new Error("No SKU is available for Secondary Mumbai DOI validation.");
const secondaryMumbaiStock = secondaryMumbaiSku.locs.find(location => location.n === "Mumbai").s;
const expectedSecondaryMumbaiDoi = Math.round(secondaryMumbaiStock / secondaryMumbaiSku.secMumbaiDRR);
if (secondaryMumbaiSku.secMumbaiDOI !== expectedSecondaryMumbaiDoi) {
  throw new Error(`Secondary Mumbai DOI mismatch: ${secondaryMumbaiSku.secMumbaiDOI} != ${expectedSecondaryMumbaiDoi}`);
}

if (!html.includes("fg_chat_log_v1")) throw new Error("Persistent chat key changed.");
if (/control tower|dashboard/i.test(html)) throw new Error("Dashboard language leaked into the chat-first interface.");

console.log(JSON.stringify({
  status: "passed",
  reportDate: FG_DATA.reportDate,
  skus: FG_DATA.skus.length,
  rows: FG_DATA.rowCount,
  commands: ["summary", "help", "natural SKU", "context location", "B2B Mumbai location", "near expiry", "out of stock", "secondary sales", "channel level sale", "last 2 months", "named month", "channel DRR", "SKU channel DRR"],
}));
