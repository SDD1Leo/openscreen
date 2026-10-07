import type { CursorRecordingSample, NativeCursorType } from "../../../../src/native/contracts";

/** One line from `openscreen-x11-cursor.py` (see electron/native/linux-capture). */
export interface LinuxCursorSampleEvent {
	type: "sample";
	timestampMs: number;
	/** X screen pixels (physical). */
	x: number;
	y: number;
	visible: boolean;
	handle: string | null;
	cursorType?: string | null;
	leftButtonDown?: boolean;
	asset: null;
}

export type LinuxCursorEvent =
	| LinuxCursorSampleEvent
	| { type: "ready"; timestampMs: number }
	| { type: "error"; timestampMs: number; message: string };

interface Bounds {
	x: number;
	y: number;
	width: number;
	height: number;
}

const KNOWN_CURSOR_TYPES: ReadonlySet<NativeCursorType> = new Set<NativeCursorType>([
	"arrow",
	"text",
	"pointer",
	"crosshair",
	"open-hand",
	"closed-hand",
	"resize-ew",
	"resize-ns",
	"resize-nesw",
	"resize-nwse",
	"move",
	"not-allowed",
	"wait",
	"app-starting",
	"help",
	"up-arrow",
]);

function toCursorType(value: string | null | undefined): NativeCursorType | null {
	return value && KNOWN_CURSOR_TYPES.has(value as NativeCursorType)
		? (value as NativeCursorType)
		: null;
}

/**
 * Turn a helper sample into the editor's normalized sample.
 *
 * The helper reports X screen pixels; Electron's display bounds are DIPs. On X11 a single scale
 * factor applies to the whole screen, so dividing by it puts both in the same space. Clicks are
 * derived from left-button transitions, the same rule the Windows session uses.
 */
export function normalizeLinuxCursorSample(input: {
	payload: LinuxCursorSampleEvent;
	bounds: Bounds;
	scaleFactor: number;
	startTimeMs: number;
	previousLeftButtonDown: boolean;
}): { sample: CursorRecordingSample; withinBounds: boolean; leftButtonDown: boolean } {
	const { payload, bounds, startTimeMs, previousLeftButtonDown } = input;
	const scale = input.scaleFactor > 0 ? input.scaleFactor : 1;
	const width = Math.max(1, bounds.width);
	const height = Math.max(1, bounds.height);
	const cx = (payload.x / scale - bounds.x) / width;
	const cy = (payload.y / scale - bounds.y) / height;
	const withinBounds = cx >= 0 && cx <= 1 && cy >= 0 && cy <= 1;
	const leftButtonDown = payload.leftButtonDown === true;
	const interactionType =
		leftButtonDown && !previousLeftButtonDown
			? "click"
			: !leftButtonDown && previousLeftButtonDown
				? "mouseup"
				: "move";

	return {
		withinBounds,
		leftButtonDown,
		sample: {
			timeMs: Math.max(0, payload.timestampMs - startTimeMs),
			cx,
			cy,
			// No captured cursor images on Linux. An assetId would make the editor look one up,
			// find nothing and draw nothing; without one it draws its bundled art for cursorType.
			assetId: null,
			visible: payload.visible && withinBounds,
			cursorType: toCursorType(payload.cursorType),
			interactionType,
		},
	};
}
