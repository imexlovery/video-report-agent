// Observe Pi's provider boundary without recording payloads or credentials.
export default function (pi) {
  let sequence = 0;
  let request;
  // Use the supported RPC notification channel: Pi guards extension stdout.
  const emit = (ctx, event) => ctx.ui.notify("video-report-trace:" + JSON.stringify(event), "info");
  pi.on("before_provider_request", (event, ctx) => {
    request = { id: ++sequence, clock: performance.now(), first: false };
    emit(ctx, { type: "request_start", request_id: request.id,
      timestamp: Date.now() / 1000, boundary: "before_provider_request",
      provider: ctx.model?.provider, model: ctx.model?.id,
      input_bytes: new TextEncoder().encode(JSON.stringify(event.payload)).length });
  });
  pi.on("message_update", (event, ctx) => {
    const update = event.assistantMessageEvent;
    if (!request || request.first || !update
      || !["thinking_delta", "text_delta", "toolcall_delta"].includes(update.type)
      || typeof update.delta !== "string" || update.delta.length === 0) return;
    request.first = true;
    emit(ctx, { type: "request_first_response", request_id: request.id,
      timestamp: Date.now() / 1000, response_kind: update.type,
      latency_ms: performance.now() - request.clock });
  });
  pi.on("message_end", (event) => {
    if (event.message?.role === "assistant") request = undefined;
  });
}
