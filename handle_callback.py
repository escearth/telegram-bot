# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data
    message_id = call.message.message_id

    # ── Force-join verification ────────────────────────────────────────
    if data == "check_join":
        if not REQUIRED_CHANNEL:
            bot.answer_callback_query(call.id, "No channel configured.")
            return
        with _joined_cache_lock:
            _joined_cache.pop(user_id, None)
        joined = _is_joined_channel(user_id)
        if joined is True:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "✅ Welcome! You're verified.")
            # Re-send welcome
            name = call.from_user.first_name or "there"
            import sqlite3 as _sq
            with db_lock:
                _c = _sq.connect(DB_FILE)
                cur = _c.cursor()
                cur.execute("SELECT lang FROM user_languages WHERE user_id=?", (user_id,))
                _row = cur.fetchone()
                _c.close()
            if _row is None:
                _send_language_picker(call.message.chat.id)
            else:
                bot.send_message(
                    call.message.chat.id,
                    T(user_id, 'start_welcome', name=name),
                    parse_mode='HTML'
                )
        else:
            bot.answer_callback_query(call.id, "❌ Not yet joined. Please join the channel first.", show_alert=True)
        return

    # ⭐ ADMIN ACTIONS - Must be BEFORE security check
    if data == "admin_broadcast":
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only", show_alert=True)
            return

        set_user_state(user_id, 'admin_broadcast')
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                "📢 <b>Broadcast Message</b>\n\n"
                "Send the message you want to broadcast to all users.\n\n"
                "<i>This will be sent to ALL users who have used the bot.</i>\n\n"
                "To cancel, send /cancel",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except:
            pass
        return

    if data == "admin_clear_cache":
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only", show_alert=True)
            return

        with _cache_lock:
            _cache.clear()
        bot.answer_callback_query(call.id, "✅ Cache cleared!", show_alert=True)
        logger.info(f"Cache cleared by owner {user_id}")
        return

    if data == "admin_stats":
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only", show_alert=True)
            return

        # Get detailed stats
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()

            # Users by language
            c.execute("SELECT lang, COUNT(*) FROM user_languages GROUP BY lang")
            lang_stats = c.fetchall()

            # Top crypto alerts
            c.execute("""
                SELECT crypto_id, COUNT(*) as cnt
                FROM alerts
                GROUP BY crypto_id
                ORDER BY cnt DESC
                LIMIT 5
            """)
            top_alerts = c.fetchall()

            conn.close()

        msg = "📊 <b>Detailed Statistics</b>\n\n"
        msg += "<b>Users by Language:</b>\n"
        for lang, count in lang_stats:
            lang_name = "English" if lang == 'en' else "Persian"
            msg += f"  {lang_name}: {count}\n"

        msg += "\n<b>Top Alert Coins:</b>\n"
        for crypto_id, count in top_alerts:
            name = CRYPTO_LIST.get(crypto_id, crypto_id)
            if '(' in name:
                sym = _sym(crypto_id)
            else:
                sym = crypto_id.upper()
            msg += f"  {sym}: {count} alerts\n"

        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                add_timestamp(msg),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except:
            pass
        return

    # ⭐ SECURITY: Check panel ownership (except for language selection)
    if not data.startswith("set_lang_"):
        cleanup_expired_panels()

        if not check_panel_owner(message_id, user_id):
            bot.answer_callback_query(
                call.id,
                "⚠️ This panel belongs to another user.\n"
                "این پنل متعلق به کاربر دیگری است.",
                show_alert=True
            )
            return

    # ── Language selection ────────────────────────────────────────────
    if data in ("set_lang_en", "set_lang_fa"):
        lang = data.split("_")[2]  # 'en' or 'fa'
        db_set_lang(user_id, lang)
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        # Send confirmation in the NEW language
        toast = T(user_id, 'lang_set_en') if lang == 'en' else T(user_id, 'lang_set_fa')
        bot.send_message(call.message.chat.id, toast, parse_mode='HTML')
        # Force-join check before showing welcome
        if REQUIRED_CHANNEL and _is_joined_channel(user_id) is False:
            _send_join_required(call.message.chat.id)
            return
        # Then immediately show /start welcome
        name = call.from_user.first_name or "there"
        bot.send_message(
            call.message.chat.id,
            T(user_id, 'start_welcome', name=name),
            parse_mode='HTML'
        )
        logger.info(f"User {user_id} set language to {lang}")
        return

    # ── Convert wizard ────────────────────────────────────────────────
    if data.startswith("cvt1_"):
        from_cid = data[5:]
        from_sym = _sym(from_cid)
        coins = list(CRYPTO_LIST.keys()) + ['usd', 'toman']
        rows = []
        row = []
        for cid in coins:
            if cid == from_cid:
                continue
            sym = _sym(cid)
            row.append(types.InlineKeyboardButton(sym, callback_data=f"cvt2_{from_cid}_{cid}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'convert_step2', sym=from_sym),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup(rows)
            )
        except Exception:
            pass
        return

    if data.startswith("cvt2_"):
        _, from_cid, to_cid = data.split("_", 2)
        from_sym = _sym(from_cid)
        to_sym   = _sym(to_cid)
        set_user_state(user_id, f"convert_{from_cid}_{to_cid}")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'convert_step3', from_sym=from_sym, to_sym=to_sym),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, "btn_cvt_cancel"), callback_data="cvt_cancel")
                ]])
            )
        except Exception:
            pass
        return

    if data == "cvt_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        if user_id in user_state:
            del_user_state(user_id)
        return

    # ── Stars amount calculator ───────────────────────────────────
    if data == "stars_calc":
        set_user_state(user_id, 'stars_amount')
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'stars_prompt'),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, "btn_cvt_cancel"), callback_data="stars_cancel")
                ]])
            )
        except Exception:
            pass
        return

    if data == "stars_cancel":
        bot.answer_callback_query(call.id)
        del_user_state(user_id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    # ── Compare coin picker (step 1 & 2) ─────────────────────────────
    if data.startswith("cmp1_"):
        cid1 = data[5:]
        filtered = [cid for cid in CRYPTO_LIST if cid != cid1]
        rows = []
        row = []
        for cid in filtered:
            sym = _sym(cid)
            row.append(types.InlineKeyboardButton(sym, callback_data=f"cmp2_{cid1}_{cid}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        sym1 = _sym(cid1)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'compare_pick2', sym=sym1),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup(rows)
            )
        except Exception:
            pass
        return

    if data.startswith("cmp2_"):
        _, cid1, cid2 = data.split("_", 2)
        if cid1 not in CRYPTO_LIST or cid2 not in CRYPTO_LIST:
            bot.answer_callback_query(call.id, T(user_id, 'unknown_coin_short'))
            return
        bot.answer_callback_query(call.id, T(user_id, 'fetching'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        _do_compare(call.message, cid1, cid2, user_id)
        return

    if data.startswith("cmpref_"):
        _, cid1, cid2 = data.split("_", 2)
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, "refreshing"))
        _do_compare(call.message, cid1, cid2, user_id, edit_msg_id=call.message.message_id)
        return

    # ── Alert wizard (step 1: coin → step 2: direction → step 3: price) ──
    if data == "alrt_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "alrt_new":
        bot.answer_callback_query(call.id)
        coins = [c for c in CRYPTO_LIST.keys() if c != 'telegram-stars']
        rows = []
        for i in range(0, len(coins), 3):
            row = []
            for cid in coins[i:i+3]:
                sym = _sym(cid)
                row.append(types.InlineKeyboardButton(sym, callback_data=f"alrt1_{cid}"))
            rows.append(row)
        rows.append([types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel")])
        try:
            bot.send_message(
                call.message.chat.id,
                T(user_id, 'alert_step1'),
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup(rows)
            )
        except Exception:
            pass
        return

    if data.startswith("alrt1_"):
        cid = data[6:]
        if cid not in CRYPTO_LIST:
            bot.answer_callback_query(call.id, T(user_id, 'unknown_coin_short'))
            return
        sym = _sym(cid)
        price = get_crypto_price(cid)
        price_str = fmt_price(price) if price else "-"
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_above'), callback_data=f"alrt2_{cid}_above"),
            types.InlineKeyboardButton(T(user_id, 'btn_below'), callback_data=f"alrt2_{cid}_below"),
        ],[
            types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel"),
        ]])
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'alert_step2', sym=sym, price=price_str),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception:
            pass
        return

    if data.startswith("alrt2_"):
        parts = data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, T(user_id, 'invalid_data'))
            return
        cid, direction = parts[1], parts[2]
        sym = _sym(cid)
        price = get_crypto_price(cid)
        price_str = fmt_price(price) if price else "-"
        arrow = "📈" if direction == "above" else "📉"
        set_user_state(user_id, f"alert_price_{cid}_{direction}")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'alert_step3', sym=sym, price=price_str, arrow=arrow, direction=direction),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="alrt_cancel")
                ]])
            )
        except Exception:
            pass
        return

    if data == "show_alerts":
        bot.answer_callback_query(call.id)
        alerts = db_get_alerts(user_id)
        if not alerts:
            bot.send_message(
                call.message.chat.id,
                T(user_id, 'no_alerts_simple'),
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_set_alert'), callback_data="alrt_new")
                ]])
            )
            return
        keyboard = []
        above_w = T(user_id, 'above_word')
        below_w = T(user_id, 'below_word')
        header = T(user_id, 'alerts_header', count=len(alerts), max=MAX_ALERTS_PER_USER)
        body = []
        # Batch-fetch all alert prices at once
        alert_ids = [a['crypto_id'] for a in alerts if a['crypto_id'] in CRYPTO_LIST]
        if alert_ids:
            _fetch_prices_batch(','.join(set(alert_ids)))
        for a in alerts:
            arrow = '📈' if a['direction'] == 'above' else '📉'
            cur   = get_crypto_price(a['crypto_id'])
            dword = above_w if a['direction'] == 'above' else below_w
            if cur:
                pct_str = f"{abs((a['target_price']-cur)/cur)*100:.1f}"
                dist = f"  <i>{T(user_id, 'away_pct', pct=pct_str)}</i>"
            else:
                dist = ""
            body.append(f"{arrow} <b>{a['symbol']}</b> {dword} <b>{fmt_price(a['target_price'])}</b>{dist}")
            keyboard.append([types.InlineKeyboardButton(
                f"🗑  {a['symbol']} {dword} {fmt_price(a['target_price'])}",
                callback_data=f"alertdel_{a['id']}"
            )])
        text = header + (quote("\n".join(body)) if body else "")
        keyboard.append([
            types.InlineKeyboardButton(T(user_id, 'btn_add_alert'),  callback_data="alrt_new"),
            types.InlineKeyboardButton(T(user_id, 'btn_delete_all'), callback_data="alertdelall"),
        ])
        bot.send_message(
            call.message.chat.id,
            text,
            parse_mode='HTML',
            reply_markup=types.InlineKeyboardMarkup(keyboard)
        )
        return

    # Route alert + digest callbacks to their handler
    if data.startswith("alertdel") or data.startswith("digest_"):
        _handle_alert_callbacks(call, data, user_id)
        return

    # ── Wallet callbacks ──────────────────────
    if data == "wnoop_0" or data.startswith("wnoop_"):
        # tapping the address label does nothing
        bot.answer_callback_query(call.id)
        return

    if data.startswith("wrem_"):
        idx = int(data.split("_")[1])
        wallets = db_get_wallets(user_id)
        if idx >= len(wallets):
            bot.answer_callback_query(call.id, T(user_id, 'wallet_not_found'))
            return
        address = wallets[idx]
        db_remove_wallet(user_id, address)
        logger.info(f"User {user_id} removed wallet {address[:6]}…{address[-4:]}")
        wallets = db_get_wallets(user_id)
        try:
            bot.edit_message_text(
                wallets_message_text(wallets),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=build_wallets_keyboard(wallets)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, T(user_id, 'wallet_removed_toast'))
        return

    if data == "wadd":
        bot.answer_callback_query(call.id)
        set_user_state(user_id, 'add_wallet_inline')
        bot.send_message(call.message.chat.id, T(user_id, 'send_wallet_addr'))
        return

    if data == "hclose" or data == "wclose":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "market_refresh":
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, 'refreshing'))
        market_cmd(call.message, user_id=user_id, edit_msg_id=call.message.message_id)
        return

    # Price list refresh
    if data == "refresh_all_prices":
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, 'refreshing'))
        ids = ','.join(CRYPTO_LIST.keys())
        prices = _fetch_prices_batch(ids)
        text = _build_price_list_message(user_id, prices)
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_refresh'), callback_data="refresh_all_prices")
        ]])
        try:
            bot.edit_message_text(
                add_timestamp(text),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception:
            pass
        return

    # Price refresh button on chart messages
    if data.startswith("refresh_"):
        crypto = data[len("refresh_"):]
        allowed, msg = is_refresh_allowed(user_id)
        if not allowed:
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return
        bot.answer_callback_query(call.id, T(user_id, 'refreshing'))
        # Invalidate cache for this coin
        with _cache_lock:
            _cache.pop(crypto, None)
        price_usd = get_crypto_price(crypto)
        usd_to_irr = get_usd_to_irr()
        if not price_usd or usd_to_irr is None:
            bot.answer_callback_query(call.id, T(user_id, 'price_fetch_fail'), show_alert=True)
            return
        price_irr = price_usd * usd_to_irr
        crypto_name = CRYPTO_LIST.get(crypto, crypto.upper())
        refresh_kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_refresh'), callback_data=f"refresh_{crypto}")
        ]])
        new_caption = (f"📊 {crypto_name}\n\n💵 <b>{fmt_price(price_usd)}</b>\n"
                       + T(user_id, 'price_toman_line', irr=f"{price_irr:,.0f}"))
        new_caption = add_timestamp(new_caption)
        try:
            bot.edit_message_caption(
                caption=new_caption,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=refresh_kb
            )
        except Exception:
            pass
        return

    if data == "gdpr_delete_confirm":
        bot.answer_callback_query(call.id)
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM holdings    WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM buy_prices  WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM wallets     WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM alerts      WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM digest_prefs WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM user_languages WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
        del_user_state(user_id)
        with _lang_cache_lock:
            _lang_cache.pop(user_id, None)
        logger.info(f"GDPR delete: all data removed for user {user_id}")
        try:
            bot.edit_message_text(
                T(user_id, 'delete_done'),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except Exception:
            pass
        return

    if data == "gdpr_delete_cancel":
        bot.answer_callback_query(call.id, T(user_id, 'gdpr_cancelled'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data == "show_holdings":
        bot.answer_callback_query(call.id)
        saved = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        bot.send_message(
            call.message.chat.id,
            holdings_message_text(saved, usd_to_irr, buy_prices),
            parse_mode='HTML',
            reply_markup=build_holdings_keyboard(saved)
        )
        return

    if data == "show_wallets":
        bot.answer_callback_query(call.id)
        wallets = db_get_wallets(user_id)
        bot.send_message(
            call.message.chat.id,
            wallets_message_text(wallets),
            parse_mode='HTML',
            reply_markup=build_wallets_keyboard(wallets)
        )
        return

    if data.startswith("hnoop_"):
        bot.answer_callback_query(call.id)
        return

    if data.startswith("hrem_"):
        symbol = data[len("hrem_"):]
        bot.answer_callback_query(call.id)
        db_remove_holding(user_id, symbol)
        db_delete_buy_price(user_id, symbol)
        logger.info(f"User {user_id} removed holding {symbol}")
        holdings = db_get_holdings(user_id) or {}
        usd_to_irr = get_usd_to_irr()
        buy_prices = db_get_buy_prices(user_id)
        try:
            bot.edit_message_text(
                holdings_message_text(holdings, usd_to_irr, buy_prices),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=build_holdings_keyboard(holdings)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, T(user_id, "holding_removed_toast", sym=symbol))
        return

    if data.startswith("hedit_"):
        symbol = data[len("hedit_"):]
        bot.answer_callback_query(call.id)
        set_user_state(user_id, f'edit_holding_{symbol}')
        bot.send_message(call.message.chat.id, T(user_id, 'edit_amount_prompt', sym=symbol), parse_mode='HTML')
        return

    if data.startswith("hbuy_"):
        symbol = data[len("hbuy_"):]
        bot.answer_callback_query(call.id)
        set_user_state(user_id, f'set_buy_price_{symbol}')
        bot.send_message(call.message.chat.id, T(user_id, 'buy_price_prompt', sym=symbol), parse_mode='HTML')
        return

    if data == "hchart":
        bot.answer_callback_query(call.id, T(user_id, 'generating_chart'))
        holdings = db_get_holdings(user_id) or {}
        if not holdings:
            bot.send_message(call.message.chat.id, T(user_id, 'no_holdings_chart'))
            return
        # Fetch all prices
        prices = {}
        for symbol in holdings:
            cid = detect_currency(symbol.lower())
            if cid:
                p = get_crypto_price(cid)
                if p:
                    prices[cid] = p
        try:
            img = get_portfolio_chart_image(holdings, prices, user_id)
            bot.send_photo(
                call.message.chat.id,
                photo=BytesIO(img),
                caption=T(user_id, 'chart_caption'),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Portfolio chart failed: {e}")
            bot.send_message(call.message.chat.id, T(user_id, 'chart_fail'))
        return

    if data == "hpick_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        if user_id in user_state:
            del_user_state(user_id)
        return

    if data.startswith("hpick_"):
        cid = data[6:]
        if cid not in CRYPTO_LIST:
            bot.answer_callback_query(call.id, T(user_id, 'unknown_coin_short'))
            return
        sym   = _sym(cid)
        price = get_crypto_price(cid)
        price_str = T(user_id, 'now_price', price=fmt_price(price)) if price else ""
        set_user_state(user_id, f"hpick_amount_{cid}")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'coin_amount_prompt', sym=sym, price=price_str),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=types.InlineKeyboardMarkup([[
                    types.InlineKeyboardButton(T(user_id, 'btn_cancel'), callback_data="hpick_cancel")
                ]])
            )
        except Exception:
            pass
        return

    if data == "hadd":
        bot.answer_callback_query(call.id)
        _show_holding_coin_picker(
            call.message.chat.id,
            T(user_id, 'add_coin_prompt'),
            user_id
        )
        return

    if data == "hclearall":
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(T(user_id, 'btn_yes_clear'), callback_data="hclearall_confirm"),
            types.InlineKeyboardButton(T(user_id, 'btn_cancel'),    callback_data="hclearall_cancel"),
        ]])
        bot.send_message(
            call.message.chat.id,
            T(user_id, 'clear_all_prompt'),
            parse_mode='HTML',
            reply_markup=kb
        )
        return

    if data == "hclearall_confirm":
        bot.answer_callback_query(call.id)
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
        try:
            bot.edit_message_text(
                T(user_id, 'holdings_cleared'),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except Exception:
            pass
        return

    if data == "hclearall_cancel":
        bot.answer_callback_query(call.id, T(user_id, 'cancelled'))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    # ── Chart coin picker (step 1) → time range buttons ─────
    if data.startswith("chr1_"):
        cid = data[5:]
        if cid not in CRYPTO_LIST:
            bot.answer_callback_query(call.id, T(user_id, 'unknown_coin_short'))
            return
        sym = _sym(cid)
        kb = types.InlineKeyboardMarkup(row_width=4)
        kb.add(*[types.InlineKeyboardButton(d, callback_data=f"chart_{cid}_{d}") for d in CHART_DAYS])
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                T(user_id, 'chart_pick_days', sym=sym),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception:
            pass
        return

    # ── Chart range selector ──────────────────────────────────
    if data.startswith("chart_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            _, crypto, days_label = parts
            days = CHART_DAYS.get(days_label, 30)
            bot.answer_callback_query(call.id, T(user_id, 'generating_chart'))
            try:
                img_bytes, symbol = get_crypto_chart_image(crypto, days, user_id)
                price = get_crypto_price(crypto)
                kb = types.InlineKeyboardMarkup(row_width=4)
                kb.add(*[types.InlineKeyboardButton(d, callback_data=f"chart_{crypto}_{d}") for d in CHART_DAYS])
                caption = f"📊 <b>{symbol}</b> - {days}d"
                if price:
                    caption += f"\n💵 <b>{fmt_price(price)}</b>"
                if call.message.photo:
                    bot.edit_message_media(
                        types.InputMediaPhoto(BytesIO(img_bytes), caption=add_timestamp(caption), parse_mode='HTML'),
                        chat_id=call.message.chat.id, message_id=call.message.message_id,
                        reply_markup=kb
                    )
                else:
                    try:
                        bot.delete_message(call.message.chat.id, call.message.message_id)
                    except Exception:
                        pass
                    bot.send_photo(
                        call.message.chat.id, photo=BytesIO(img_bytes),
                        caption=add_timestamp(caption), parse_mode='HTML', reply_markup=kb
                    )
            except Exception as e:
                logger.error(f"Chart callback failed: {e}")
        return

    # ── Exchange selector ─────────────────────────────────────
    if data.startswith("setex_"):
        exch = data.split("_", 1)[1]
        if exch in EXCHANGES or exch == 'coingecko':
            set_user_exchange(user_id, exch)
            name = EXCHANGE_NAMES.get(exch, 'CoinGecko')
            bot.answer_callback_query(call.id, f"✅ Source set to {name}")
            try:
                bot.edit_message_text(
                    f"✅ Default price source set to <b>{name}</b>",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode='HTML'
                )
            except Exception:
                pass
        return

    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['clearwallets'])
@rate_limit_check
@loading_indicator
def clear_wallets(message):
    user_id = message.from_user.id
    if db_clear_wallets(user_id):
        bot.reply_to(message, T(user_id, 'all_wallets_removed'))
        logger.info(f"User {user_id} cleared wallets")
    else:
