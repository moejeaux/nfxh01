/**
 * ACP v2 buyer sidecar: @virtuals-protocol/acp-node-v2 + HTTP for Python (DegenClawAcp).
 * Logs prefixed with ACP_ for grep/ops.
 */
import "dotenv/config";
import express from "express";
import { base, baseSepolia } from "viem/chains";
import {
  AcpAgent,
  AlchemyEvmProviderAdapter,
  AssetToken,
  PrivyAlchemyEvmProviderAdapter,
} from "@virtuals-protocol/acp-node-v2";
import type { BudgetSetEvent, JobRoomEntry } from "@virtuals-protocol/acp-node-v2";

function log(msg: string, extra?: Record<string, unknown>): void {
  if (extra) {
    console.log(`ACP_ ${msg}`, extra);
  } else {
    console.log(`ACP_ ${msg}`);
  }
}

async function buildProvider() {
  const wallet = process.env.ACP_AGENT_WALLET_ADDRESS?.trim() as `0x${string}` | undefined;
  if (!wallet) {
    throw new Error("ACP_AGENT_WALLET_ADDRESS is required");
  }

  const testnet = process.env.ACP_USE_TESTNET === "true";
  const chains = testnet ? [baseSepolia] : [base];

  const privyWid = process.env.ACP_PRIVY_WALLET_ID?.trim();
  const privySigner = process.env.ACP_PRIVY_SIGNER_PRIVATE_KEY?.trim();
  if (privyWid && privySigner) {
    log("provider=PrivyAlchemyEvmProviderAdapter");
    return PrivyAlchemyEvmProviderAdapter.create({
      walletAddress: wallet,
      walletId: privyWid,
      signerPrivateKey: privySigner,
      chains,
    });
  }

  const pk = process.env.ACP_ALCHEMY_PRIVATE_KEY?.trim() as `0x${string}` | undefined;
  const entityId = Number(process.env.ACP_ALCHEMY_ENTITY_ID ?? "1");
  if (pk) {
    log("provider=AlchemyEvmProviderAdapter");
    return AlchemyEvmProviderAdapter.create({
      walletAddress: wallet,
      privateKey: pk,
      entityId,
      chains,
    });
  }

  throw new Error(
    "Set ACP_ALCHEMY_PRIVATE_KEY or (ACP_PRIVY_WALLET_ID + ACP_PRIVY_SIGNER_PRIVATE_KEY)",
  );
}

function fundAmountUsdc(
  entry: JobRoomEntry,
  fallback: number,
): number {
  if (entry.kind !== "system") return fallback;
  if (entry.event.type === "budget.set") {
    const ev = entry.event as BudgetSetEvent;
    if (typeof ev.amount === "number" && ev.amount > 0) {
      return ev.amount;
    }
  }
  return fallback;
}

async function main(): Promise<void> {
  const fallbackFund = Number(process.env.ACP_JOB_FUND_USDC ?? "0.1");
  const autoComplete =
    process.env.ACP_AUTO_COMPLETE !== "false" &&
    process.env.ACP_AUTO_COMPLETE !== "0";

  const provider = await buildProvider();
  const agent = await AcpAgent.create({ provider });

  agent.on("entry", async (session, entry) => {
    if (entry.kind !== "system") return;
    const ev = entry.event;
    try {
      if (ev.type === "budget.set") {
        const usdc = fundAmountUsdc(entry, fallbackFund);
        log(`budget.set job=${session.jobId} fund_usdc=${usdc}`);
        await session.fund(AssetToken.usdc(usdc, session.chainId));
      } else if (ev.type === "job.submitted" && autoComplete) {
        if (!session.roles.includes("evaluator")) {
          log(`job.submitted skip_complete (not evaluator) job=${session.jobId}`);
          return;
        }
        log(`job.submitted complete job=${session.jobId}`);
        await session.complete("ACP buyer auto-approved");
      }
    } catch (err) {
      console.error("ACP_ entry_handler_error", err);
    }
  });

  await agent.start(() => {
    log("agent_sse_connected");
  });

  const supported = agent.getSupportedChainIds();
  const chainId = Number(
    process.env.ACP_CHAIN_ID ?? supported[0] ?? (process.env.ACP_USE_TESTNET === "true" ? 84532 : 8453),
  );

  const defaultOffering = process.env.ACP_OFFERING_NAME ?? "perp_trade";
  const defaultProvider = process.env.ACP_PROVIDER_WALLET?.trim() ?? "";
  const port = Number(process.env.ACP_BUYER_PORT ?? "8765");
  const sharedSecret = process.env.ACP_BUYER_SECRET?.trim();

  const app = express();
  app.use(express.json({ limit: "512kb" }));

  app.get("/health", (_req, res) => {
    res.json({
      ok: true,
      chainId,
      supportedChainIds: supported,
      offeringNameDefault: defaultOffering,
    });
  });

  app.post("/v1/job", async (req, res) => {
    if (sharedSecret) {
      const tok = req.header("x-acp-buyer-token") ?? req.header("X-Acp-Buyer-Token");
      if (tok !== sharedSecret) {
        res.status(401).json({ ok: false, error: "unauthorized" });
        return;
      }
    }

    const body = req.body as Record<string, unknown> | undefined;
    const offeringName =
      (typeof body?.offeringName === "string" && body.offeringName) || defaultOffering;
    const providerAddress = String(
      body?.providerAddress ?? defaultProvider,
    ).trim();
    if (!providerAddress) {
      res.status(400).json({ ok: false, error: "providerAddress required" });
      return;
    }

    const requirementData =
      (body?.requirementData as Record<string, unknown> | undefined) ??
      (() => {
        const { offeringName: _o, providerAddress: _p, ...rest } = body ?? {};
        return rest as Record<string, unknown>;
      })();

    try {
      const buyerAddr = await agent.getAddress();
      const jid = await agent.createJobByOfferingName(
        chainId,
        offeringName,
        providerAddress,
        requirementData,
        { evaluatorAddress: buyerAddr },
      );
      const idStr = jid.toString();
      log(`job_created jobId=${idStr} offering=${offeringName}`);
      res.json({ ok: true, jobId: idStr });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error("ACP_ createJobByOfferingName failed", msg);
      res.status(500).json({ ok: false, error: msg });
    }
  });

  app.listen(port, "127.0.0.1", () => {
    log(`buyer_http_listening host=127.0.0.1 port=${port}`);
  });
}

main().catch((e) => {
  console.error("ACP_ fatal", e);
  process.exit(1);
});
