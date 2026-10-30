import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const MAX_BODY_BYTES = 64 * 1024;
const DEPLOYMENT_KEY = /^[A-Za-z0-9._-]{1,128}$/;
const INCIDENT_ID = /^[A-Za-z0-9._-]{1,128}$/;

type Route = {
	deploymentDir: string;
	controlHost?: string;
	sessionId: string;
};

type FaultEvent = {
	schema_version?: number;
	deployment_key: string;
	incident_id: string;
	event_path: string;
	summary?: Record<string, unknown>;
};

let server: ReturnType<typeof createServer> | undefined;
let currentContext: ExtensionContext | undefined;
const routes = new Map<string, Route>();
const delivered = new Set<string>();

function validRelativePath(value: string): boolean {
	return (
		value.length > 0 &&
		!value.startsWith("/") &&
		!value.includes("\\\\") &&
		!value.split("/").includes("..")
	);
}

function sendJson(response: ServerResponse, status: number, payload: unknown): void {
	const body = JSON.stringify(payload);
	response.statusCode = status;
	response.setHeader("Content-Type", "application/json");
	response.setHeader("Content-Length", Buffer.byteLength(body));
	response.end(body);
}

async function readBody(request: IncomingMessage): Promise<string> {
	const chunks: Buffer[] = [];
	let size = 0;
	for await (const chunk of request) {
		const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
		size += buffer.length;
		if (size > MAX_BODY_BYTES) {
			throw new Error("request body is too large");
		}
		chunks.push(buffer);
	}
	return Buffer.concat(chunks).toString("utf8");
}

function parseFaultEvent(value: unknown): FaultEvent {
	if (!value || typeof value !== "object") {
		throw new Error("request must be an object");
	}
	const item = value as Record<string, unknown>;
	if (
		typeof item.deployment_key !== "string" ||
		!DEPLOYMENT_KEY.test(item.deployment_key)
	) {
		throw new Error("invalid deployment_key");
	}
	if (
		typeof item.incident_id !== "string" ||
		!INCIDENT_ID.test(item.incident_id)
	) {
		throw new Error("invalid incident_id");
	}
	if (typeof item.event_path !== "string" || !validRelativePath(item.event_path)) {
		throw new Error("event_path must be a relative path without traversal");
	}
	return {
		schema_version: typeof item.schema_version === "number" ? item.schema_version : 1,
		deployment_key: item.deployment_key,
		incident_id: item.incident_id,
		event_path: item.event_path,
		summary:
			item.summary && typeof item.summary === "object"
				? (item.summary as Record<string, unknown>)
				: undefined,
	};
}

async function handleFaultEvent(
	pi: ExtensionAPI,
	request: IncomingMessage,
	response: ServerResponse,
): Promise<void> {
	let event: FaultEvent;
	try {
		event = parseFaultEvent(JSON.parse(await readBody(request)));
	} catch (error) {
		sendJson(response, 400, {
			accepted: false,
			status: "invalid_request",
			error: error instanceof Error ? error.message : String(error),
		});
		return;
	}
	const route = routes.get(event.deployment_key);
	if (!route) {
		sendJson(response, 404, {
			accepted: false,
			status: "deployment_not_registered",
		});
		return;
	}
	const deliveryKey = event.deployment_key + ":" + event.incident_id;
	if (delivered.has(deliveryKey)) {
		sendJson(response, 202, {
			accepted: true,
			status: "duplicate",
			incident_id: event.incident_id,
		});
		return;
	}
	if (!currentContext) {
		sendJson(response, 503, { accepted: false, status: "session_unavailable" });
		return;
	}
	try {
		pi.sendMessage(
			{
				customType: "vllm-fault-event",
				content:
					"检测到新的 vLLM 故障事件。" +
					"请读取事件文件、部署清单和相关日志，" +
					"按照故障恢复 Skill 完成复核、恢复和验收。" +
					"事件编号：" +
					event.incident_id +
					"；事件文件：" +
					event.event_path,
				display: true,
				details: {
					deployment_key: event.deployment_key,
					incident_id: event.incident_id,
					event_path: event.event_path,
					summary: event.summary,
				},
			},
			{
				triggerTurn: true,
				deliverAs: "followUp",
			},
		);
		delivered.add(deliveryKey);
		sendJson(response, 202, {
			accepted: true,
			status: "queued",
			incident_id: event.incident_id,
			session_id: route.sessionId,
		});
	} catch (error) {
		sendJson(response, 503, {
			accepted: false,
			status: "session_unavailable",
			error: error instanceof Error ? error.message : String(error),
		});
	}
}

function startServer(pi: ExtensionAPI): void {
	if (server) return;
	const host = process.env.VLLM_FAULT_NOTIFY_HOST ?? "127.0.0.1";
	const port = Number(process.env.VLLM_FAULT_NOTIFY_PORT ?? "8765");
	server = createServer((request, response) => {
		if (request.method === "GET" && request.url === "/health") {
			sendJson(response, 200, {
				status: currentContext ? "ready" : "starting",
				session_id: currentContext?.sessionManager.getSessionId(),
				deployment_keys: [...routes.keys()],
			});
			return;
		}
		if (request.method === "POST" && request.url === "/v1/fault-events") {
			void handleFaultEvent(pi, request, response);
			return;
		}
		sendJson(response, 404, { accepted: false, status: "not_found" });
	});
	server.listen(port, host);
}

async function stopServer(): Promise<void> {
	const active = server;
	server = undefined;
	if (!active) return;
	await new Promise<void>((resolve) => active.close(() => resolve()));
}

export default function vllmFaultEvents(pi: ExtensionAPI): void {
	pi.registerTool({
		name: "vllm_fault_register_deployment",
		label: "Register vLLM fault route",
		description:
			"Register a deployment key so the current Pi session can receive its fault events.",
		parameters: Type.Object({
			deployment_key: Type.String(),
			deployment_dir: Type.String(),
			control_host: Type.Optional(Type.String()),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const sessionId = ctx.sessionManager.getSessionId();
			routes.set(params.deployment_key, {
				deploymentDir: params.deployment_dir,
				controlHost: params.control_host,
				sessionId,
			});
			return {
				content: [
					{
						type: "text",
						text:
							"已登记故障通知路由：" +
							params.deployment_key +
							"，当前 session：" +
							sessionId,
					},
				],
				details: {
					deployment_key: params.deployment_key,
					session_id: sessionId,
				},
			};
		},
	});

	pi.on("session_start", async (_event, ctx) => {
		currentContext = ctx;
		startServer(pi);
	});

	pi.on("session_shutdown", async () => {
		await stopServer();
		currentContext = undefined;
		routes.clear();
		delivered.clear();
	});
}
