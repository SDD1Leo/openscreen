import { type ChildProcessByStdio, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import type { Readable } from "node:stream";
import { app, type Rectangle, screen } from "electron";
import type { CursorRecordingData, CursorRecordingSample } from "../../../../src/native/contracts";
import { type LinuxCursorEvent, normalizeLinuxCursorSample } from "./linuxCursorSample";
import type { CursorRecordingSession } from "./session";
import { TelemetryRecordingSession } from "./telemetryRecordingSession";

interface LinuxNativeCursorRecordingSessionOptions {
	getDisplayBounds: () => Rectangle | null;
	maxSamples: number;
	sampleIntervalMs: number;
	startTimeMs?: number;
}

const READY_TIMEOUT_MS = 5_000;

function findCursorHelperPath(): string | null {
	const envPath = process.env.OPENSCREEN_X11_CURSOR_HELPER?.trim();
	const appPath = join(
		app.getAppPath(),
		"electron",
		"native",
		"linux-capture",
		"openscreen-x11-cursor.py",
	);
	const candidates = [
		envPath,
		app.isPackaged ? appPath.replace(/\.asar([/\\])/, ".asar.unpacked$1") : appPath,
		app.isPackaged
			? join(
					process.resourcesPath,
					"electron",
					"native",
					"linux-capture",
					"openscreen-x11-cursor.py",
				)
			: null,
	].filter((c): c is string => Boolean(c));
	return candidates.find((c) => existsSync(c)) ?? null;
}

/**
 * Linux (X11) cursor recording with the same output as the Windows/macOS sessions: position,
 * which cursor is showing (arrow / hand / I-beam …) and click events, so the editor can draw its
 * own cursor. Runs `openscreen-x11-cursor.py`; if that cannot start (Wayland, no Python, no X
 * display) it falls back to position-only telemetry rather than failing the recording.
 */
export class LinuxNativeCursorRecordingSession implements CursorRecordingSession {
	private samples: CursorRecordingSample[] = [];
	private process: ChildProcessByStdio<null, Readable, Readable> | null = null;
	private lineBuffer = "";
	private startTimeMs = 0;
	private previousLeftButtonDown = false;
	private fallback: TelemetryRecordingSession | null = null;
	private readyResolve: (() => void) | null = null;
	private readyReject: ((error: Error) => void) | null = null;
	private readyTimer: NodeJS.Timeout | null = null;

	constructor(private readonly options: LinuxNativeCursorRecordingSessionOptions) {}

	async start(): Promise<void> {
		this.samples = [];
		this.lineBuffer = "";
		this.previousLeftButtonDown = false;
		this.fallback = null;
		this.startTimeMs = this.options.startTimeMs ?? Date.now();

		try {
			await this.startHelper();
		} catch (error) {
			console.warn(
				"[cursor-native][linux] X11 cursor helper unavailable; recording position only:",
				error instanceof Error ? error.message : error,
			);
			this.killHelper();
			this.fallback = new TelemetryRecordingSession({
				getDisplayBounds: this.options.getDisplayBounds,
				maxSamples: this.options.maxSamples,
				sampleIntervalMs: this.options.sampleIntervalMs,
				startTimeMs: this.startTimeMs,
			});
			await this.fallback.start();
		}
	}

	async stop(): Promise<CursorRecordingData> {
		if (this.fallback) {
			const data = await this.fallback.stop();
			this.fallback = null;
			return data;
		}
		this.clearReady();
		this.killHelper();
		console.info(
			"[cursor-native][linux]",
			JSON.stringify({ event: "stop", sampleCount: this.samples.length }),
		);
		// No system cursor images on Linux: the editor draws its own art from each sample's
		// cursorType, which is exactly what provider "none" with no assets tells it to do.
		return { version: 2, provider: "none", samples: this.samples, assets: [] };
	}

	private startHelper(): Promise<void> {
		const helperPath = findCursorHelperPath();
		if (!helperPath) {
			return Promise.reject(new Error("openscreen-x11-cursor.py not found"));
		}
		const child = spawn("python3", [helperPath, String(this.options.sampleIntervalMs)], {
			stdio: ["ignore", "pipe", "pipe"],
		});
		this.process = child;
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => this.handleChunk(chunk));
		child.stderr.setEncoding("utf8");
		child.stderr.on("data", (chunk: string) => {
			const message = chunk.trim();
			if (message) console.error("[cursor-native][linux]", message);
		});
		child.once("error", (error) => this.rejectReady(error));
		child.once("exit", (code, signal) => {
			this.rejectReady(
				new Error(`X11 cursor helper exited before ready (code=${code}, signal=${signal})`),
			);
		});

		return new Promise<void>((resolve, reject) => {
			this.readyResolve = resolve;
			this.readyReject = reject;
			this.readyTimer = setTimeout(() => {
				this.rejectReady(new Error("Timed out waiting for the X11 cursor helper"));
			}, READY_TIMEOUT_MS);
		});
	}

	private handleChunk(chunk: string) {
		this.lineBuffer += chunk;
		const lines = this.lineBuffer.split(/\r?\n/);
		this.lineBuffer = lines.pop() ?? "";
		for (const line of lines) {
			const trimmed = line.trim();
			if (!trimmed) continue;
			let event: LinuxCursorEvent;
			try {
				event = JSON.parse(trimmed) as LinuxCursorEvent;
			} catch {
				continue;
			}
			if (event.type === "ready") {
				this.resolveReady();
			} else if (event.type === "error") {
				this.rejectReady(new Error(event.message));
			} else if (event.type === "sample") {
				this.addSample(event);
			}
		}
	}

	private addSample(event: Extract<LinuxCursorEvent, { type: "sample" }>) {
		// One scale factor covers the whole X screen, so the primary display's is the one.
		const scaleFactor = screen.getPrimaryDisplay().scaleFactor || 1;
		const bounds =
			this.options.getDisplayBounds() ??
			screen.getDisplayNearestPoint({
				x: Math.round(event.x / scaleFactor),
				y: Math.round(event.y / scaleFactor),
			}).bounds;
		const normalized = normalizeLinuxCursorSample({
			payload: event,
			bounds,
			scaleFactor,
			startTimeMs: this.startTimeMs,
			previousLeftButtonDown: this.previousLeftButtonDown,
		});
		this.previousLeftButtonDown = normalized.leftButtonDown;
		this.samples.push(normalized.sample);
		if (this.samples.length > this.options.maxSamples) {
			this.samples.shift();
		}
	}

	private resolveReady() {
		const resolve = this.readyResolve;
		this.clearReady();
		resolve?.();
	}

	private rejectReady(error: Error) {
		const reject = this.readyReject;
		this.clearReady();
		reject?.(error);
	}

	private clearReady() {
		if (this.readyTimer) {
			clearTimeout(this.readyTimer);
			this.readyTimer = null;
		}
		this.readyResolve = null;
		this.readyReject = null;
	}

	private killHelper() {
		const child = this.process;
		this.process = null;
		if (child && !child.killed) {
			child.kill();
		}
	}
}
