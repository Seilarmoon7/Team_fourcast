/*
 * Personal shopper: the chat column, its receipts, the activity strip, and the room it
 * builds at the top of the page.
 *
 * Every turn goes to the shopping agent (POST {agent}/api/chat). The agent returns its
 * reply, the tools it called, products by SKU, and a checkout link. Each SKU is looked up
 * in the storefront for its real image, link, price and stock.
 *
 * "Demo as" sends a known demo customer id so the agent loads that customer's profile.
 * A signed-in customer is identified by their account email instead.
 */
(() => {
  const root = document.getElementById('Shopper');
  if (!root) return;

  const AGENT = (root.dataset.agent || '').replace(/\/$/, '');
  const ACCOUNT_EMAIL = root.dataset.email || null;
  // The room is built on the homepage only; other pages keep their own content.
  const ON_HOME = root.dataset.template === 'index';

  const PN = { google: 'Gemini', databricks: 'Databricks', bloomreach: 'Bloomreach', shopify: 'Shopify' };
  // Which platform each agent tool touches.
  const TOOL_MAP = {
    get_customer_profile: [['bloomreach', 'profile + recs']],
    save_preferences: [['bloomreach', 'customer properties']],
    find_products: [['bloomreach', 'catalog search'], ['shopify', 'live price + stock']],
    get_personal_picks: [['bloomreach', 'personalized picks'], ['shopify', 'live price + stock']],
    request_campaign: [['bloomreach', 'campaign_request']],
    create_checkout: [['shopify', 'cart permalink'], ['bloomreach', 'checkout event']],
    databricks_profile: [['databricks', 'customer_profile']],
  };
  const platformsFor = (tool) => TOOL_MAP[tool] || [['google', tool]];

  // Demo customers the agent knows by id. Guest is an anonymous visitor.
  const SHOPPERS = { guest: ACCOUNT_EMAIL ? 'You' : 'Guest', lashawna: 'Lashawna', elene: 'Elene', neal: 'Neal', rozanne: 'Rozanne' };
  const STARTERS = ['I need to redo my living room, it feels cold', 'Scandinavian bedroom under $500', 'A reading corner for a small apartment'];

  const MAILS = [
    { day: 'Day 1', subject: 'Your order is confirmed', preview: 'Order {order}. Arriving Thursday.' },
    { day: 'Day 2', subject: 'On its way', preview: 'Your {item} shipped this morning.' },
    { day: 'Day 3', subject: 'Living with what you chose', preview: 'A short note on care, so it ages the way it should.' },
    { day: 'Day 4', subject: 'Two more that finish the room', preview: 'Picked from what you bought, not from a catalog.' },
    { day: 'Day 5', subject: 'How did it land?', preview: 'One question, thirty seconds.' },
  ];

  const $ = (sel, el = root) => el.querySelector(sel);
  const log = $('[data-log]'), input = $('[data-input]'), send = $('[data-send]'), form = $('[data-form]');
  const strip = document.querySelector('[data-strip]'), ticker = strip.querySelector('[data-ticker]');
  const room = document.getElementById('ShopperRoom');
  const page = document.querySelector('.shopper-layout__page') || document.scrollingElement;

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  // [text](url) becomes a link; a bare URL becomes a short "link" so it can't stretch the bubble.
  const links = (s) => s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<]+)/g,
    (m, text, url, bare) => `<a href="${url || bare}" target="_top">${text || 'link'}</a>`);
  const inlineMd = (s) => links(esc(s)).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>');
  const md = (s) => String(s ?? '').trim().replace(/^[ \t]*[*-][ \t]+/gm, '• ').split(/\n{2,}/)
    .filter((p) => p.trim()).map((p) => '<p>' + inlineMd(p).replace(/\n/g, '<br>') + '</p>').join('');
  // The checkout button carries the checkout link, so drop the copy the agent writes into its reply.
  const withoutCartLinks = (s) => String(s ?? '').replace(/\[[^\]]*\]\((https?:\/\/[^\s)]*\/cart\/[^\s)]*)\)|https?:\/\/\S*\/cart\/\S*/g, '');
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const money = (n) => '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: Number(n) % 1 ? 2 : 0, maximumFractionDigits: 2 });
  const toBottom = (el) => { el.scrollTop = el.scrollHeight; requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; }); };
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem('shopper:' + k)); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem('shopper:' + k, JSON.stringify(v)); } catch (e) {} },
  };

  // Conversation state survives page navigation within the tab.
  let S = store.get('state') || { shopper: 'guest', sid: null, email: null, profile: null, prefs: {}, picks: [], chosen: [], lastAsk: '', log: '' };
  S.chosen = (S.chosen || []).filter((p) => p && p.sku);   // older saves kept SKUs only
  let busy = false;
  const save = () => { S.log = log.innerHTML; store.set('state', S); };

  /* ---------- chat ---------- */
  function lamp(p, on) { strip.querySelector(`[data-p="${p}"]`)?.classList.toggle('is-hot', on); }
  function tick(p, call, result) { ticker.innerHTML = `<b>${esc(PN[p] || p)}</b> · ${esc(call)}${result ? ` → <em>${esc(result)}</em>` : ''}`; }
  function addMsg(who, html) {
    const d = document.createElement('div');
    d.className = `shopper__msg shopper__msg--${who}`;
    d.innerHTML = html;
    log.appendChild(d); toBottom(log);
    return d;
  }
  // Storefront product for an agent SKU: predictive search finds it, the variant list confirms it.
  async function lookup(sku) {
    try {
      const q = new URLSearchParams({ q: sku, 'resources[type]': 'product', 'resources[limit]': '3', 'resources[options][fields]': 'variants.sku' });
      const hits = (await (await fetch('/search/suggest.json?' + q)).json()).resources.results.products || [];
      for (const h of hits) {
        const p = await (await fetch(`/products/${h.handle}.js`)).json();
        const v = p.variants.find((x) => x.sku === sku);
        if (v) return { title: p.title, price: v.price / 100, available: v.available, variant: v.id, url: p.url, image: p.featured_image };
      }
    } catch (e) {}
    return {};
  }
  // Tool arguments in words: checkout items by product name, lists joined, no raw JSON.
  const nameOf = (sku) => ([...S.chosen, ...S.picks].find((p) => p.sku === sku) || {}).title || sku;
  const argVal = (v) => Array.isArray(v)
    ? v.map((x) => (x && x.sku ? nameOf(x.sku) + (x.quantity > 1 ? ` ×${x.quantity}` : '') : typeof x === 'object' ? argVal(x) : x)).join(', ')
    : v && typeof v === 'object' ? Object.entries(v).map(([k, x]) => `${k} ${x}`).join(', ') : v;
  const argText = (args) => Object.entries(args || {}).filter(([k]) => k !== 'email')
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${argVal(v)}`).join(' · ');

  async function playTools(tools) {
    for (const t of tools) {
      const ps = platformsFor(t.tool);
      ps.forEach(([p, what]) => { lamp(p, true); tick(p, what, argText(t.args)); });
      await wait(260);
      ps.forEach(([p]) => lamp(p, false));
    }
  }
  function addReceipt(tools, note) {
    const plats = [...new Set(tools.flatMap((t) => platformsFor(t.tool).map(([p]) => PN[p] || p)))].join(', ');
    const r = document.createElement('div');
    r.className = 'shopper__receipt';
    r.innerHTML = `<button type="button" class="shopper__chip" data-receipt><b>›</b>${tools.length} call${tools.length > 1 ? 's' : ''} · ${esc(plats)}</button>
      <div class="shopper__rbody">${tools.map((t) =>
        `<div class="shopper__rrow"><span class="shopper__rp">${esc(PN[platformsFor(t.tool)[0][0]] || 'Agent')}</span><span><span class="shopper__rc">${esc(t.tool)}${t.ok ? '' : ' · failed'}</span><span class="shopper__rv">${esc(argText(t.args) || 'no arguments')}</span></span><span class="shopper__rms">${t.n != null ? esc(t.n) + ' found' : ''}</span></div>`).join('')}
      ${note ? `<div class="shopper__rnote">${esc(note)}</div>` : ''}</div>`;
    log.appendChild(r); toBottom(log);
  }
  function offerNote(profile) {
    if (!profile || profile.churn_risk == null) return '';
    const churn = Number(profile.churn_risk), thr = Number(profile.churn_risk_threshold ?? 0.6);
    if (profile.retention_offer) return `Churn ${churn.toFixed(3)} is at or above ${thr}, so ${profile.retention_offer.code} is attached in code. The model never decides discounts.`;
    return `Churn ${churn.toFixed(3)} is below ${thr}. No code.`;
  }

  async function ask(message) {
    message = (message || '').trim();
    if (!message || busy) return;
    busy = true; send.disabled = true; input.value = '';
    log.querySelector('.shopper__starters')?.remove();
    if (!S.lastAsk) S.lastAsk = message;   // the room's brief is how the shopper first described it
    addMsg('me', `<div class="shopper__bub">${esc(message)}</div>`);
    const typing = addMsg('bot', '<div class="shopper__bub"><div class="shopper__typing"><i></i><i></i><i></i></div></div>');
    lamp('google', true); tick('google', 'gemini', 'thinking');
    try {
      const r = await fetch(AGENT + '/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, session_id: S.sid, email: S.email || ACCOUNT_EMAIL, shopper: S.shopper === 'guest' ? null : S.shopper }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'HTTP ' + r.status);
      lamp('google', false);

      S.sid = d.session_id || S.sid;
      if (d.email) S.email = d.email;
      const firstProfile = !S.profile && d.profile;
      if (d.profile) S.profile = d.profile;
      const tools = Array.isArray(d.tools) ? d.tools.slice() : [];
      // The profile lookup reads Databricks features; the agent reports that as a source, not a tool.
      if (firstProfile && (d.profile.source || []).some((s) => String(s).startsWith('databricks'))) {
        tools.splice(tools.findIndex((t) => t.tool === 'get_customer_profile') + 1, 0, { tool: 'databricks_profile', args: {}, ok: true, n: null });
      }
      tools.forEach((t) => {
        if (t.tool === 'save_preferences' && t.args) Object.assign(S.prefs, t.args);
        else if (t.args && t.args.room && !S.prefs.room) S.prefs.room = t.args.room;
      });

      await playTools(tools);
      typing.innerHTML = `<div class="shopper__bub">${md((d.checkout_url ? withoutCartLinks(d.reply) : d.reply) || '…')}</div>`;
      if (tools.length) addReceipt(tools, firstProfile ? offerNote(d.profile) : '');

      roomUpdate();
      if (Array.isArray(d.products) && d.products.length) await roomPicks(d.products);
      if (d.checkout_url) addCheckout(d.checkout_url, tools);
      ticker.textContent = 'idle · listening';
    } catch (err) {
      lamp('google', false);
      const why = err instanceof TypeError ? "Can't reach the shopping agent from this site." : err.message;
      typing.innerHTML = `<div class="shopper__bub shopper__bub--err">Something went wrong (${esc(why)}). Try again in a moment.</div>`;
      ticker.textContent = 'idle · agent unreachable';
    } finally {
      busy = false; send.disabled = false; save(); input.focus();
    }
  }

  function addCheckout(url, tools) {
    const ck = tools.find((t) => t.tool === 'create_checkout');
    const items = ((ck && ck.args && ck.args.items) || []).map((it) => ({ ...([...S.chosen, ...S.picks].find((p) => p.sku === it.sku) || { title: it.sku, price: 0 }), qty: it.quantity || 1 }));
    const sub = items.reduce((a, p) => a + Number(p.price || 0) * p.qty, 0);
    const offer = S.profile && S.profile.retention_offer;
    const d = addMsg('bot', `<a class="shopper__pay" href="${esc(url)}">Go to checkout${sub ? ' · ' + money(offer ? sub * 0.85 : sub) : ''} →</a>
      <button type="button" class="shopper__sim" data-simulate>Simulate payment</button>`);
    d.querySelector('[data-simulate]').dataset.items = JSON.stringify(items);
  }

  /* Demo only: plays what happens after Shopify reports the order paid. */
  async function simulatePaid(btn) {
    if (busy) return;
    busy = true; btn.disabled = true; btn.textContent = 'Paid ✓';
    const items = JSON.parse(btn.dataset.items || '[]');
    const order = '#' + (1000 + Math.floor(Math.random() * 900));
    lamp('shopify', true); lamp('bloomreach', true); tick('shopify', 'order paid', `${order} · webhook → Bloomreach`);
    await wait(700); lamp('shopify', false); lamp('bloomreach', false);
    addMsg('bot', "<div class=\"shopper__bub\">Paid, thank you. I'll keep you posted on delivery, and check in once it's all in the room.</div>");
    const box = roomPaid(order);
    for (const m of MAILS) {
      await wait(650);
      lamp('bloomreach', true); tick('bloomreach', 'nurture', m.subject);
      const preview = m.preview.replace('{order}', order).replace('{item}', items[0] ? items[0].title : 'order');
      box.insertAdjacentHTML('beforeend', `<div class="shopper-room__mail"><div class="shopper-room__day">${m.day}</div><div><div class="shopper-room__subject">${esc(m.subject)}</div><div class="shopper-room__preview">${esc(preview)}</div></div></div>`);
      await wait(220); lamp('bloomreach', false);
    }
    ticker.textContent = 'idle · journey complete';
    busy = false; save();
  }

  /* ---------- room ---------- */
  // The room shows what the agent picked this turn; the shopper adds pieces to a running list.
  const firstName = () => (S.shopper === 'guest' ? (S.profile && S.profile.first_name) || 'you' : SHOPPERS[S.shopper]);
  function roomTitle() {
    const where = S.prefs.room || ([...S.chosen, ...S.picks].find((p) => p.room) || {}).room;
    return where ? `${firstName() === 'you' ? 'Your' : firstName() + "'s"} ${where}` : 'Your room';
  }
  function roomSub() {
    const bits = [S.prefs.style, S.prefs.budget_max ? `under ${money(S.prefs.budget_max)}` : null].filter(Boolean);
    return bits.length ? bits.join(' · ') : S.lastAsk ? `“${S.lastAsk}”` : '';
  }
  function roomShell() {
    if (!room.hidden) return;
    room.closest('main').classList.add('shopper-has-room');
    room.hidden = false;
    room.innerHTML = `<div class="shopper-room__intro"><div class="shopper-room__top"><div class="shopper-room__eyebrow">Built for ${esc(firstName())}</div>
      <button type="button" class="shopper-room__restart" data-restart>Start over</button></div>
      <h2 class="shopper-room__title"></h2><p class="shopper-room__sub"></p><div data-offer></div><div data-list></div></div>
      <div data-band></div><div data-after></div>`;
    page.scrollTop = 0;
  }
  function roomUpdate() {
    if (!ON_HOME) return;
    if (!S.profile && !Object.keys(S.prefs).length && !S.picks.length) return;
    render();
  }
  async function roomPicks(products) {
    S.picks = await Promise.all(products.map(async (p) => ({ ...p, ...(await lookup(p.sku)), sku: p.sku })));
    if (!ON_HOME) {
      addMsg('bot', '<a class="shopper__pay" href="/">See your room →</a>');
      return;
    }
    await render(true);
  }
  async function render(animate = false) {
    if (!ON_HOME) return;
    roomShell();
    room.querySelector('.shopper-room__title').textContent = roomTitle();
    room.querySelector('.shopper-room__sub').textContent = roomSub();
    const offer = S.profile && S.profile.retention_offer;
    room.querySelector('[data-offer]').innerHTML = offer
      ? `<div class="shopper-room__offer"><b>${esc(offer.code)}</b>Applied automatically when you check out.</div>` : '';

    const band = room.querySelector('[data-band]');
    if (!S.picks.length) band.innerHTML = '';
    else {
      const n = S.picks.length;
      band.innerHTML = `<div class="shopper-room__band${animate ? ' is-new' : ''}"><div class="shopper-room__bandhead">
        <h3>Picked for you</h3><span>${n} piece${n > 1 ? 's' : ''}</span></div><div class="shopper-room__grid"></div></div>`;
      const grid = band.querySelector('.shopper-room__grid');
      for (const p of S.picks) {
        const on = S.chosen.some((c) => c.sku === p.sku);
        const bg = p.image ? `background-image:url('${encodeURI(p.image)}')` : 'background:#8a7a66';
        grid.insertAdjacentHTML('beforeend', `<div class="shopper-room__card${on ? ' is-chosen' : ''}${animate ? ' is-new' : ''}" data-sku="${esc(p.sku)}">
          <a href="${esc(p.url || '#')}"><div class="shopper-room__img" style="${bg}">${p.image ? '' : esc([p.material, p.color].filter(Boolean).join(' · '))}</div></a>
          <div class="shopper-room__name">${esc(p.title)}</div><div class="shopper-room__price">${money(p.price)}</div>
          <div class="shopper-room__why">${esc([p.style, p.material].filter(Boolean).join(' · '))}</div>
          ${on ? '<div class="shopper-room__tag">✓ In your room</div>' : ''}
          <button type="button" class="shopper-room__pick" data-pick>${on ? 'Remove' : 'Add to your room'}</button></div>`);
        if (animate) await wait(170);
      }
    }

    // Everything chosen so far: what the shopper will check out.
    const total = S.chosen.reduce((a, p) => a + Number(p.price || 0), 0);
    const c = S.chosen.length;
    room.querySelector('[data-list]').innerHTML = c ? `<div class="shopper-room__list">
      <div class="shopper-room__listhead">Your room so far</div>
      ${S.chosen.map((p) => `<div class="shopper-room__row">
        <a class="shopper-room__rowname" href="${esc(p.url || '#')}">${esc(p.title)}</a>
        <span class="shopper-room__rowprice">${money(p.price)}</span>
        <button type="button" class="shopper-room__remove" data-remove="${esc(p.sku)}">Remove</button></div>`).join('')}
      <div class="shopper-room__listfoot"><span>${c} piece${c > 1 ? 's' : ''}</span><b>${money(total)}</b>
        <button type="button" class="shopper-room__buy" data-buy>Buy your room →</button></div></div>` : '';
  }
  function roomPaid(order) {
    if (!ON_HOME) return document.createElement('div');
    const a = room.querySelector('[data-after]');
    a.innerHTML = `<div class="shopper-room__after"><h3>Order ${esc(order)}, what happens next</h3><div data-mails></div></div>`;
    return a.querySelector('[data-mails]');
  }

  /* ---------- wiring ---------- */
  function starters() {
    const d = addMsg('bot', `<div class="shopper__bub">${S.shopper === 'guest'
      ? "Describe a room, a style or a budget and I'll put together pieces you can buy right here."
      : `Shopping as ${esc(SHOPPERS[S.shopper])}. Same agent, but it reads their customer profile and changes how it sells.`}</div>`);
    d.insertAdjacentHTML('afterend', `<div class="shopper__starters">${STARTERS.map((s) => `<button type="button" data-starter>${esc(s)}</button>`).join('')}</div>`);
  }
  function reset(k) {
    S = { shopper: k, sid: null, email: null, profile: null, prefs: {}, picks: [], chosen: [], lastAsk: '', log: '' };
    log.innerHTML = '';
    strip.querySelectorAll('.is-hot').forEach((l) => l.classList.remove('is-hot'));
    ticker.textContent = 'idle · waiting for a shopper';
    room.hidden = true; room.innerHTML = '';
    room.closest('main').classList.remove('shopper-has-room');
    markShopper(); starters(); save();
  }
  function markShopper() {
    root.querySelectorAll('[data-viewas] button').forEach((b) => b.setAttribute('aria-pressed', b.dataset.k === S.shopper));
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); ask(input.value); });
  log.addEventListener('click', (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    if (t.hasAttribute('data-starter')) ask(t.textContent);
    else if (t.hasAttribute('data-receipt')) { t.parentElement.classList.toggle('is-open'); toBottom(log); }
    else if (t.hasAttribute('data-simulate')) simulatePaid(t);
  });
  room.addEventListener('click', (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    if (t.hasAttribute('data-restart')) { if (!busy) reset(S.shopper); return; }
    if (t.hasAttribute('data-remove')) {
      S.chosen = S.chosen.filter((p) => p.sku !== t.dataset.remove);
      render(); save();
    } else if (t.hasAttribute('data-pick')) {
      const sku = t.closest('[data-sku]').dataset.sku;
      S.chosen = S.chosen.some((p) => p.sku === sku) ? S.chosen.filter((p) => p.sku !== sku) : [...S.chosen, S.picks.find((p) => p.sku === sku)];
      render(); save();
    } else if (t.hasAttribute('data-buy')) {
      ask(`I'd like to buy: ${S.chosen.map((p) => `${p.title} (${p.sku})`).join(', ')}. Please create my checkout.`);
    }
  });
  Object.entries(SHOPPERS).forEach(([k, label]) => {
    const b = document.createElement('button');
    b.type = 'button'; b.dataset.k = k; b.textContent = label;
    b.onclick = () => { if (!busy) reset(k); };
    $('[data-viewas]').appendChild(b);
  });

  // Restore the conversation after navigating to another page, or start fresh.
  if (S.log) {
    log.innerHTML = S.log; toBottom(log); markShopper();
    roomUpdate();
  } else reset(S.shopper);
})();
