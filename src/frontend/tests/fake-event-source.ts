import { act } from "@testing-library/react";

/** A controllable stand-in for the browser's EventSource. */
export class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  private named = new Map<string, (e: MessageEvent<string>) => void>();
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent<string>) => void) {
    this.named.set(type, fn);
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
  // --- driving it ---
  open() {
    act(() => {
      this.readyState = FakeEventSource.OPEN;
      this.onopen?.();
    });
  }
  emit(seq: number, type: string, payload: Record<string, unknown> = {}, createdAt = "2030-01-01T00:00:00Z") {
    act(() => this.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify({ seq, type, payload, created_at: createdAt }),
    })));
  }
  stage(seq: number, state: string) {
    this.emit(seq, "stage", { state });
  }
  end(state: "COMPLETE" | "FAILED") {
    act(() => this.named.get("end")?.(new MessageEvent("end", { data: JSON.stringify({ run_id: "run_1", state }) })));
  }
  drop(givenUp = false) {
    act(() => {
      this.readyState = givenUp ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
      this.onerror?.();
    });
  }
}
