let currentCampaignId = null;
let simStage = 0; // 0: uninitialized, 1: SCO sent, 2: Buyer Bid, 3: Supplier Quote, 4: Closed
let hardFloor = 50.0;
let softTarget = 120.0;
let maxRounds = 3;

const PRESETS = {
  buyer_low: {
    role: "buyer",
    email: "Subject: Firm Offer - Basmati 1121\\n\\nDear Desk,\\n\\nWe review your SCO and can only offer USD 995.00/MT CIF Jebel Ali for 500 MT. This yields less than minimum floor.\\n\\nRegards,\\nProcurement Manager\\nGulf Food Trading LLC"
  },
  buyer_concession_r1: {
    role: "buyer",
    email: "Subject: Revised Target CIF - Basmati 1121\\n\\nDear Desk,\\n\\nWe are interested in the 500 MT Basmati parcel. Our target CIF price is USD 1045.00/MT CIF Jebel Ali. Payment 100% LC at sight.\\n\\nWarm regards,\\nProcurement Manager\\nGulf Food Trading LLC"
  },
  supplier_quote: {
    role: "supplier",
    email: "Subject: Quotation - Basmati 1121 Sella\\n\\nDear Desk,\\n\\nIn response to your RFQ, we confirm volume allocation of 500 MT Basmati 1121 at USD 900.00/MT FOB Karachi. Ready for prompt shipment against LC.\\n\\nBest regards,\\nIndus Rice Mills"
  },
  buyer_viable: {
    role: "buyer",
    email: "Subject: Accepted Terms - Basmati 1121\\n\\nDear Desk,\\n\\nWe accept your revised terms and agree to increase our purchase bid for 500 MT Basmati 1121 to USD 1150.00/MT CIF Jebel Ali. Please confirm volume lock and send PI.\\n\\nWarm regards,\\nGulf Food Trading LLC"
  }
};

function loadPreset(key) {
  const p = PRESETS[key];
  if (!p) return;
  document.querySelector(`input[name="senderRole"][value="${p.role}"]`).checked = true;
  document.getElementById("emailInput").value = p.email.replace(/\\n/g, '\n');
}

function updatePipelineUI(step, action = null) {
  for (let i = 1; i <= 4; i++) {
    const el = document.getElementById(`pipeStep${i}`);
    const icon = document.getElementById(`pipeStep${i}Icon`);
    if (i < step) {
      el.className = "p-3.5 rounded-xl border border-emerald-700/60 bg-emerald-950/30 transition-all duration-300 relative";
      icon.className = "w-6 h-6 rounded-full bg-emerald-50 text-slate-950 text-xs font-bold flex items-center justify-center";
      icon.innerHTML = "✓";
    } else if (i === step) {
      el.className = "p-3.5 rounded-xl border border-blue-500 bg-slate-900 shadow-lg shadow-blue-500/10 transition-all duration-300 relative ring-1 ring-blue-500";
      icon.className = "w-6 h-6 rounded-full bg-blue-500 text-white text-xs font-bold flex items-center justify-center animate-pulse";
      icon.innerHTML = i;
    } else {
      el.className = "p-3.5 rounded-xl border border-slate-800 bg-slate-950/60 transition-all duration-300 relative opacity-50";
      icon.className = "w-6 h-6 rounded-full bg-slate-800 text-slate-400 text-xs font-bold flex items-center justify-center";
      icon.innerHTML = i;
    }
  }

  const actionBadge = document.getElementById("activeActionBadge");
  const stratAction = document.getElementById("strategicAction");
  if (action === "REJECT_HARD") {
    actionBadge.textContent = "Action: REJECT_HARD";
    actionBadge.className = "px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-rose-950 text-rose-300 border border-rose-800";
    stratAction.textContent = "REJECT_HARD";
    stratAction.className = "text-xs font-extrabold text-rose-400 mt-1 block tracking-tight";
  } else if (action === "COUNTER_TO_MAXIMIZE") {
    actionBadge.textContent = "Action: COUNTER_TO_MAXIMIZE";
    actionBadge.className = "px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-amber-950 text-amber-300 border border-amber-800";
    stratAction.textContent = "COUNTER";
    stratAction.className = "text-xs font-extrabold text-amber-400 mt-1 block tracking-tight";
  } else if (action === "ACCEPT_AND_CLOSE") {
    actionBadge.textContent = "Action: ACCEPT_AND_CLOSE";
    actionBadge.className = "px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-950 text-emerald-300 border border-emerald-800 animate-pulse";
    stratAction.textContent = "CLOSED 🎉";
    stratAction.className = "text-xs font-extrabold text-emerald-400 mt-1 block tracking-tight";
  }
}

function updateSpreadProgress(spread) {
  document.getElementById("netSpread").textContent = `$${spread.toFixed(2)}/MT`;
  document.getElementById("progressText").textContent = `$${Math.max(0, spread).toFixed(2)} / $${softTarget.toFixed(2)} MT`;
  const pct = Math.min(100, Math.max(0, (spread / softTarget) * 100));
  document.getElementById("progressBar").style.width = `${pct}%`;
}

// Launch Campaign (Proactive Origination & Autonomous Execution)
async function launchCampaign(autoRun = false) {
  hardFloor = parseFloat(document.getElementById("hardFloorInput").value);
  softTarget = parseFloat(document.getElementById("softTargetInput").value);
  maxRounds = parseInt(document.getElementById("maxRoundsInput").value);

  const payload = {
    commodity: document.getElementById("commoditySelect").value,
    target_volume_mt: parseFloat(document.getElementById("volumeInput").value),
    target_margin_pct: 10.0,
    max_variance_from_benchmark_pct: 5.0,
    destination_port: document.getElementById("portSelect").value,
    min_profit_per_mt_hard: hardFloor,
    min_profit_per_mt_soft: softTarget,
    max_negotiation_rounds: maxRounds,
    auto_run: autoRun,
  };

  try {
    const res = await fetch("/api/campaigns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    currentCampaignId = data.campaign_id;

    // Render discovered counterparties
    if (data.discovered_buyers) {
      const buyersList = document.getElementById("buyersList");
      buyersList.innerHTML = data.discovered_buyers.map(b => `
        <div class="bg-slate-950 p-2 rounded-lg border border-slate-800 flex justify-between items-center">
          <div>
            <span class="font-medium text-white">${b.name}</span>
            <div class="text-[10px] text-slate-400">${b.primary_port} • ${b.country}</div>
          </div>
          <span class="text-[10px] font-bold text-emerald-400">★ ${b.reputation_score}</span>
        </div>
      `).join("");
    }

    if (data.discovered_suppliers) {
      const suppliersList = document.getElementById("suppliersList");
      suppliersList.innerHTML = data.discovered_suppliers.map(s => `
        <div class="bg-slate-950 p-2 rounded-lg border border-slate-800 flex justify-between items-center">
          <div>
            <span class="font-medium text-white">${s.name}</span>
            <div class="text-[10px] text-slate-400">${s.primary_port} • ${s.country}</div>
          </div>
          <span class="text-[10px] font-bold text-blue-400">★ ${s.reputation_score}</span>
        </div>
      `).join("");
    }
    document.getElementById("counterpartiesCard").classList.remove("hidden");

    if (data.dynamic_fob_ceiling) {
      document.getElementById("fobCeiling").textContent = `$${data.dynamic_fob_ceiling.toFixed(2)}/MT`;
    }
    if (data.dynamic_cif_floor) {
      document.getElementById("cifFloor").textContent = `$${data.dynamic_cif_floor.toFixed(2)}/MT`;
    }

    if (autoRun && data.deal_status === "closed") {
      // Autonomous execution completed end-to-end
      simStage = 4;
      document.getElementById("simulationCard").classList.remove("opacity-40", "pointer-events-none");
      document.getElementById("dealBadge").textContent = `Closed & Locked: ${data.campaign_id} 🎉`;
      document.getElementById("dealBadge").className = "px-2.5 py-1 rounded-lg text-xs font-semibold bg-emerald-600 text-white animate-pulse";
      document.getElementById("pipelineStatusLabel").textContent = `Step 4: Deal Closed Autonomously (Rounds: ${data.negotiation_rounds_completed})`;
      document.getElementById("turnCounterBadge").textContent = `Rounds: ${data.negotiation_rounds_completed} / ${maxRounds}`;
      document.getElementById("invariantText").textContent = data.evaluation_reason || "Autonomous negotiation concluded with optimal margin allocation.";

      updateSpreadProgress(data.final_net_spread_usd || data.net_spread_usd || 0.0);
      updatePipelineUI(4, data.action || "ACCEPT_AND_CLOSE");

      if (data.buyer_draft) {
        document.getElementById("buyerDraft").textContent = data.buyer_draft;
        document.getElementById("buyerDraftType").textContent = "Deal Confirmation (SCO Acceptance)";
      }
      if (data.supplier_draft) {
        document.getElementById("supplierDraft").textContent = data.supplier_draft;
        document.getElementById("supplierDraftType").textContent = "Volume Allocation Locked";
      }

      document.getElementById("nextTurnBtnText").textContent = "Deal Fully Closed & Optimized 🎉";
      document.getElementById("stepHint").textContent = `Autonomous execution completed (${data.audit_transcript?.length || 0} msgs in audit trail)`;
    } else {
      // Step-by-step interactive mode
      simStage = 1;
      document.getElementById("simulationCard").classList.remove("opacity-40", "pointer-events-none");
      document.getElementById("dealBadge").textContent = `Active: ${data.campaign_id}`;
      document.getElementById("dealBadge").className = "px-2.5 py-1 rounded-lg text-xs font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800";
      document.getElementById("pipelineStatusLabel").textContent = "Step 1: Proactive Buyer SCO Dispatched";

      if (data.buyer_draft) {
        document.getElementById("buyerDraft").textContent = data.buyer_draft;
        document.getElementById("buyerDraftType").textContent = `Cold SCO (Anchor $${data.anchor_cif_usd?.toFixed(2) || '0.00'}/MT)`;
      }

      updatePipelineUI(1);
      updateSpreadProgress(0.0);

      document.getElementById("nextTurnBtnText").textContent = "Simulate Turn 1: Buyer Indicates Bid ($1045 CIF) ➔";
      document.getElementById("stepHint").textContent = "Buyer responds to Cold SCO";
    }
  } catch (err) {
    alert("Error creating campaign: " + err);
  }
}

// Automated Stepper Function
async function simulateNextTurn() {
  if (!currentCampaignId) return alert("Please launch campaign first.");

  let role = "buyer";
  let email = "";

  if (simStage === 1) {
    // Step 1 -> Buyer sends initial bid ($1045 CIF, $80 spread over landed $965)
    role = "buyer";
    email = "Subject: Re: SCO Basmati 1121\n\nDear Trading Desk,\n\nWe review your SCO. We are prepared to book 500 MT Basmati 1121 at USD 1045.00/MT CIF Jebel Ali, LC at sight.\n\nRegards,\nProcurement Manager\nGulf Food Trading LLC";
    document.getElementById("nextTurnBtnText").textContent = "Simulate Turn 2: Asian Mill Quotes ($900 FOB) ➔";
    document.getElementById("stepHint").textContent = "Supplier responds to RFQ";
    simStage = 2;
  } else if (simStage === 2) {
    // Step 2 -> Supplier quotes $900 FOB
    role = "supplier";
    email = "Subject: Quotation - 500 MT Basmati 1121\n\nDear Procurement Desk,\n\nWe quote 500 MT Basmati 1121 at USD 900.00/MT FOB Karachi. LC payment.\n\nBest regards,\nIndus Rice Mills";
    document.getElementById("nextTurnBtnText").textContent = "Simulate Turn 3: Buyer Concedes to Margin Target ($1085 CIF) ➔";
    document.getElementById("stepHint").textContent = "Buyer responds to Round 1 Counter";
    simStage = 3;
  } else if (simStage === 3) {
    // Step 3 -> Buyer agrees to optimal close ($1085 CIF, $120 spread)
    role = "buyer";
    email = "Subject: Final Price Agreement - Basmati 1121\n\nDear Trading Desk,\n\nTo conclude this business, we agree to adjust our CIF price to USD 1085.00/MT CIF Jebel Ali for 500 MT. Please lock allocation and dispatch contract.\n\nWarm regards,\nGulf Food Trading LLC";
    document.getElementById("nextTurnBtnText").textContent = "Deal Fully Closed & Optimized 🎉";
    document.getElementById("stepHint").textContent = "Negotiation complete";
    simStage = 4;
  } else {
    alert("Deal negotiation cycle complete! You can test manual scenarios or launch another campaign.");
    return;
  }

  await executeNegotiationTurn(role, email);
}

async function executeNegotiationTurn(role, email) {
  const payload = {
    campaign_id: currentCampaignId,
    sender_role: role,
    raw_email: email
  };

  try {
    const res = await fetch("/api/negotiate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    // Update Round & Deal status
    document.getElementById("turnCounterBadge").textContent = `Round ${data.negotiation_round} / ${maxRounds}`;
    document.getElementById("invariantText").textContent = data.evaluation_reason;

    // Update Spread & Action
    updateSpreadProgress(data.net_spread_usd || 0.0);
    updatePipelineUI(data.pipeline_step || 2, data.action);

    if (data.deal_status === "closed") {
      document.getElementById("dealBadge").textContent = "Deal Closed & Viable! 🎉";
      document.getElementById("dealBadge").className = "px-2.5 py-1 rounded-lg text-xs font-semibold bg-emerald-600 text-white animate-pulse";
      document.getElementById("pipelineStatusLabel").textContent = "Step 4: Margin Optimized & Deal Closed";
    } else if (data.deal_status === "rejected") {
      document.getElementById("dealBadge").textContent = "Deal Rejected (Hard Floor)";
      document.getElementById("dealBadge").className = "px-2.5 py-1 rounded-lg text-xs font-semibold bg-rose-900 text-rose-200 border border-rose-700";
      document.getElementById("pipelineStatusLabel").textContent = "Deal Terminated (Hard Floor Breach)";
    } else {
      document.getElementById("dealBadge").textContent = `Round ${data.negotiation_round}: ${data.action || "Active"}`;
      document.getElementById("dealBadge").className = "px-2.5 py-1 rounded-lg text-xs font-semibold bg-amber-900/80 text-amber-200 border border-amber-700";
      document.getElementById("pipelineStatusLabel").textContent = `Step ${data.pipeline_step}: Active Bargaining Round ${data.negotiation_round}`;
    }

    // Update Correspondence Drafts
    if (data.buyer_draft) {
      document.getElementById("buyerDraft").textContent = data.buyer_draft;
      document.getElementById("buyerDraftType").textContent = data.deal_status === "closed" ? "Deal Confirmation (SCO Lock)" : "Tactical Counter-Offer";
    }
    if (data.supplier_draft) {
      document.getElementById("supplierDraft").textContent = data.supplier_draft;
      document.getElementById("supplierDraftType").textContent = data.deal_status === "closed" ? "Allocation Locked" : (data.target_fob_ceiling ? `RFQ (Ceiling $${data.target_fob_ceiling.toFixed(2)}/MT)` : "Tactical Counter-Bid");
    }

  } catch (err) {
    alert("Error executing negotiation turn: " + err);
  }
}

// Direct Form Submission
document.getElementById("negotiateForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!currentCampaignId) return alert("Please launch a campaign first.");
  const role = document.querySelector('input[name="senderRole"]:checked').value;
  const raw = document.getElementById("emailInput").value;
  if (!raw.trim()) return alert("Please enter email text.");
  await executeNegotiationTurn(role, raw);
});
