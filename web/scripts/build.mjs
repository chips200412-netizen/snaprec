import { spawnSync } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(scriptDirectory, "..");
const viteCli = join(webRoot, "node_modules", "vite", "bin", "vite.js");
const outputDirectory = join(webRoot, "dist");
const temporaryOutput = await mkdtemp(
  join(tmpdir(), "video-knowledge-web-build-"),
);

async function copyTree(source, destination) {
  await mkdir(destination, { recursive: true });
  for (const entry of await readdir(source, { withFileTypes: true })) {
    const sourcePath = join(source, entry.name);
    const destinationPath = join(destination, entry.name);
    if (entry.isDirectory()) {
      await copyTree(sourcePath, destinationPath);
    } else if (entry.isFile()) {
      await writeFile(destinationPath, await readFile(sourcePath));
    }
  }
}

try {
  const result = spawnSync(
    process.execPath,
    [
      viteCli,
      "build",
      "--configLoader",
      "native",
      "--outDir",
      temporaryOutput,
      "--emptyOutDir",
    ],
    {
      cwd: webRoot,
      env: process.env,
      stdio: "inherit",
    },
  );

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.exitCode = result.status ?? 1;
  } else {
    await rm(outputDirectory, { recursive: true, force: true });
    await copyTree(temporaryOutput, outputDirectory);
  }
} finally {
  await rm(temporaryOutput, { recursive: true, force: true });
}
