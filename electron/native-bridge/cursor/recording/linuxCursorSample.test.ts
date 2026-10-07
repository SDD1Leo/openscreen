import { describe, expect, it } from "vitest";
import { resolveInterpolatedNativeCursorFrame } from "../../../../src/lib/cursor/nativeCursor";
import { normalizeLinuxCursorSample } from "./linuxCursorSample";

// The X11 helper reports X screen pixels; Electron's display bounds are DIPs. On X11 one scale
// factor covers the whole screen (GNOME at 125% here: 1920x1080 physical = 1536x864 DIP).
const primary = { x: 0, y: 0, width: 1536, height: 864 };

function sample(over: Record<string, unknown> = {}) {
	return {
		type: "sample" as const,
		timestampMs: 10_500,
		x: 1069,
		y: 999,
		visible: true,
		handle: "41",
		cursorType: "pointer" as const,
		leftButtonDown: false,
		asset: null,
		...over,
	};
}

describe("normalizeLinuxCursorSample", () => {
	it("maps physical pixels into the selected display's 0..1 space", () => {
		const { sample: s, withinBounds } = normalizeLinuxCursorSample({
			payload: sample(),
			bounds: primary,
			scaleFactor: 1.25,
			startTimeMs: 10_000,
			previousLeftButtonDown: false,
		});
		expect(withinBounds).toBe(true);
		expect(s.cx).toBeCloseTo(855.2 / 1536, 6);
		expect(s.cy).toBeCloseTo(799.2 / 864, 6);
		expect(s.timeMs).toBe(500);
		expect(s.visible).toBe(true);
		expect(s.cursorType).toBe("pointer");
		expect(s.interactionType).toBe("move");
	});

	it("handles a second monitor to the right of the primary", () => {
		const right = { x: 1536, y: 0, width: 1920, height: 1080 };
		const { sample: s } = normalizeLinuxCursorSample({
			payload: sample({ x: 2665, y: 717 }),
			bounds: right,
			scaleFactor: 1.25,
			startTimeMs: 10_000,
			previousLeftButtonDown: false,
		});
		expect(s.cx).toBeCloseTo((2665 / 1.25 - 1536) / 1920, 6);
		expect(s.cy).toBeCloseTo(717 / 1.25 / 1080, 6);
	});

	it("hides the cursor while it is on another display", () => {
		const { sample: s, withinBounds } = normalizeLinuxCursorSample({
			payload: sample({ x: 2665 }),
			bounds: primary,
			scaleFactor: 1.25,
			startTimeMs: 10_000,
			previousLeftButtonDown: false,
		});
		expect(withinBounds).toBe(false);
		expect(s.visible).toBe(false);
	});

	it("turns left-button transitions into click and mouseup", () => {
		const base = { bounds: primary, scaleFactor: 1.25, startTimeMs: 10_000 };
		const down = normalizeLinuxCursorSample({
			...base,
			payload: sample({ leftButtonDown: true }),
			previousLeftButtonDown: false,
		});
		expect(down.sample.interactionType).toBe("click");
		expect(down.leftButtonDown).toBe(true);
		const held = normalizeLinuxCursorSample({
			...base,
			payload: sample({ leftButtonDown: true }),
			previousLeftButtonDown: true,
		});
		expect(held.sample.interactionType).toBe("move");
		const up = normalizeLinuxCursorSample({
			...base,
			payload: sample({ leftButtonDown: false }),
			previousLeftButtonDown: true,
		});
		expect(up.sample.interactionType).toBe("mouseup");
	});

	it("never reports a time before the recording started", () => {
		const { sample: s } = normalizeLinuxCursorSample({
			payload: sample({ timestampMs: 9_000 }),
			bounds: primary,
			scaleFactor: 1.25,
			startTimeMs: 10_000,
			previousLeftButtonDown: false,
		});
		expect(s.timeMs).toBe(0);
	});

	it("drops a cursor type the editor has no art for", () => {
		const { sample: s } = normalizeLinuxCursorSample({
			payload: sample({ cursorType: "banana" }),
			bounds: primary,
			scaleFactor: 1.25,
			startTimeMs: 10_000,
			previousLeftButtonDown: false,
		});
		expect(s.cursorType).toBeNull();
	});

	it("gives the editor a drawable cursor: no assetId, so it uses its own art for the type", () => {
		// Linux records no cursor images. An assetId makes the editor look one up, find nothing
		// and draw nothing (the first real recording showed exactly that); without one it draws
		// the bundled art for cursorType.
		const { sample: s } = normalizeLinuxCursorSample({
			payload: sample({ handle: "433", cursorType: "pointer" }),
			bounds: primary,
			scaleFactor: 1.25,
			startTimeMs: 10_000,
			previousLeftButtonDown: false,
		});
		expect(s.assetId ?? null).toBeNull();
		const frame = resolveInterpolatedNativeCursorFrame(
			{ version: 2, provider: "none", samples: [s], assets: [] },
			s.timeMs,
		);
		expect(frame).not.toBeNull();
		expect(frame?.asset.cursorType).toBe("pointer");
	});
});
