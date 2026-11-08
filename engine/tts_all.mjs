// Usage: node tts_all.mjs <script.json> <voice> <speed> <outdir>
// Generates one WAV per sentence, writes <outdir>/manifest.json with durations.
import fs from "node:fs";
import path from "node:path";
import { KokoroTTS } from "kokoro-js";

const [, , scriptPath, voice, speedArg, outdir] = process.argv;
const speed = Number(speedArg || 1);
const data = JSON.parse(fs.readFileSync(scriptPath, "utf8"));
fs.mkdirSync(outdir, { recursive: true });
const tts = await KokoroTTS.from_pretrained("onnx-community/Kokoro-82M-v1.0-ONNX", { dtype: "q8" });

const splitSentences = (t) =>
  t.replace(/\s+/g, " ").trim().match(/[^.!?]+[.!?]+["']?|[^.!?]+$/g).map((s) => s.trim()).filter(Boolean);

const manifest = [];
for (const sc of data.scenes) {
  const sents = splitSentences(sc.text);
  const items = [];
  for (let i = 0; i < sents.length; i++) {
    const f = path.join(outdir, `${sc.id}_${i}.wav`);
    if (!fs.existsSync(f)) {
      const a = await tts.generate(sents[i], { voice, speed });
      await a.save(f);
    }
    const buf = fs.readFileSync(f);
    // parse WAV: find 'data' chunk
    const sr = buf.readUInt32LE(24), bps = buf.readUInt16LE(34) / 8, ch = buf.readUInt16LE(22);
    let off = 12, dataLen = 0;
    while (off < buf.length) { const id = buf.toString("ascii", off, off + 4); const sz = buf.readUInt32LE(off + 4); if (id === "data") { dataLen = sz; break; } off += 8 + sz; }
    items.push({ file: f, text: sents[i], dur: dataLen / (sr * bps * ch) });
  }
  manifest.push({ id: sc.id, sentences: items });
  process.stdout.write(`${sc.id} `);
}
fs.writeFileSync(path.join(outdir, "manifest.json"), JSON.stringify(manifest, null, 1));
console.log("\ndone");
