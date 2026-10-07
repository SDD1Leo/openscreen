import type { Rectangle } from "electron";
import { LinuxNativeCursorRecordingSession } from "./linuxNativeCursorRecordingSession";
import { MacNativeCursorRecordingSession } from "./macNativeCursorRecordingSession";
import type { CursorRecordingSession } from "./session";
import { TelemetryRecordingSession } from "./telemetryRecordingSession";
import { WindowsNativeRecordingSession } from "./windowsNativeRecordingSession";

interface CreateCursorRecordingSessionOptions {
	getDisplayBounds: () => Rectangle | null;
	maxSamples: number;
	platform: NodeJS.Platform;
	sampleIntervalMs: number;
	sourceId?: string | null;
	startTimeMs?: number;
}

export function createCursorRecordingSession(
	options: CreateCursorRecordingSessionOptions,
): CursorRecordingSession {
	if (options.platform === "win32") {
		return new WindowsNativeRecordingSession({
			getDisplayBounds: options.getDisplayBounds,
			maxSamples: options.maxSamples,
			sampleIntervalMs: options.sampleIntervalMs,
			sourceId: options.sourceId,
			startTimeMs: options.startTimeMs,
		});
	}

	if (options.platform === "darwin") {
		return new MacNativeCursorRecordingSession({
			getDisplayBounds: options.getDisplayBounds,
			maxSamples: options.maxSamples,
			sampleIntervalMs: options.sampleIntervalMs,
			startTimeMs: options.startTimeMs,
		});
	}

	if (options.platform === "linux") {
		// X11: position, cursor type and clicks from the X11 helper. It falls back to the
		// position-only telemetry below by itself when it cannot run (Wayland, no Python).
		return new LinuxNativeCursorRecordingSession({
			getDisplayBounds: options.getDisplayBounds,
			maxSamples: options.maxSamples,
			sampleIntervalMs: options.sampleIntervalMs,
			startTimeMs: options.startTimeMs,
		});
	}

	// Other platforms: capture cursor positions via Electron's `screen` API on an interval.
	// No cursor sprites/assets and no clicks, just position telemetry.
	return new TelemetryRecordingSession({
		getDisplayBounds: options.getDisplayBounds,
		maxSamples: options.maxSamples,
		sampleIntervalMs: options.sampleIntervalMs,
		startTimeMs: options.startTimeMs,
	});
}
