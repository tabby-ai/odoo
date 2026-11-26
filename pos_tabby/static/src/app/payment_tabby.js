import { _t } from "@web/core/l10n/translation";
import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { QRPopup } from "@point_of_sale/app/components/popups/qr_code_popup/qr_code_popup";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

// Tabby POS flow: https://docs.tabby.ai/offline-payment-methods/pos-integration
const POLL_INTERVAL_MS = 5000; // the docs ask for 5 s
const SLOW_POLL_INTERVAL_MS = 30000; // a payment waiting for a decision in the Tabby dashboard
const DEFAULT_TIMEOUT_S = 300; // the docs ask for 300 s, never below 180 s
// Payment line statuses with a request in flight (restored as is after a reload).
export const TABBY_IN_FLIGHT = ["waiting", "waitingCard", "waitingCancel", "timeout"];

/**
 * The till only names its own order and payment line; the server keeps the
 * Tabby session, payment and refund ids (pos.tabby.attempt), so a reload of
 * the till can resume or cancel a payment, and a paid order is accepted only
 * when Tabby confirms its Tabby lines.
 */
export class PaymentTabby extends PaymentInterface {
    setup() {
        super.setup(...arguments);
        this.creating = null; // {uuid, cancelled} while the session is being created
        this.refunding = null; // uuid of the line whose refund is in flight
        this.resuming = null; // uuid of the line being resumed after a reload
        this._refundWarning = { warned: false };
        this.active = null; // the payment being followed, see _track
        // The terminal sits in a reactive store, so what this.creating and
        // this.active hold are proxies: compare them by seq, never by identity.
        this._seq = 0;
    }

    get fastPayments() {
        return false; // let the cashier adjust the amount before sending
    }

    async sendPaymentRequest(uuid) {
        super.sendPaymentRequest(uuid);
        const order = this.pos.getOrder();
        const line = order.payment_ids.find((l) => l.uuid === uuid) || order.getSelectedPaymentline();
        if (!line || !line.amount) {
            return false;
        }
        if (line.amount < 0) {
            return this._refund(order, line);
        }
        line.setPaymentStatus("waitingCard");

        const attempt = { seq: ++this._seq, uuid: line.uuid, cancelled: false };
        this.creating = attempt;
        const res = await this._rpc("tabby_create_session", {
            ...this._lineData(order, line),
            amount: line.amount,
            // Odoo 19 keeps name as "/" until the order is saved; pos_reference is the ticket number.
            order_reference: order.pos_reference || (order.name && order.name !== "/" ? order.name : order.uuid),
            items: this._orderItems(order),
            buyer: this._buyer(order),
        });
        if (this.creating?.seq === attempt.seq) {
            this.creating = null;
        }
        if (attempt.cancelled) {
            // The cashier cancelled while the session was being created. The
            // server cancels it; an order that still holds it is refused at sync.
            if (res?.payment_id) {
                this._rpc("tabby_cancel_session", this._lineData(order, line));
            }
            return false;
        }
        if (!res || res.error) {
            this._showError(res?.error || _t("Tabby is unavailable. Please use another payment method."));
            return false;
        }
        return this._track(order, line, res);
    }

    async sendPaymentCancel(order, uuid) {
        super.sendPaymentCancel(order, uuid);
        const line = order.payment_ids.find((l) => l.uuid === uuid);
        if (this.creating && this.creating.uuid === uuid) {
            this.creating.cancelled = true;
            return true;
        }
        if (this.refunding === uuid) {
            return false; // a refund in flight cannot be cancelled
        }
        if (this.active && this.active.uuid === uuid) {
            const result = await this._cancelActive(_t("Payment cancelled by the cashier."));
            return this._cancelAnswer(line, result);
        }
        if (line && line.amount > 0) {
            // Not followed in this tab (the till was reloaded): ask the server.
            const res = await this._rpc("tabby_cancel_session", this._lineData(order, line));
            if (res?.cancelled || res?.outcome === "failed") {
                return true;
            }
            this.resumeLine(order, line);
            return this._cancelAnswer(line, "unconfirmed");
        }
        return true;
    }

    // Odoo 19: true lets the line go (no money held); false keeps it, and
    // Odoo puts back the status the line had before the cancel.
    _cancelAnswer(line, result) {
        if (result === "cancelled" || result === "failed") {
            return true;
        }
        if (result === "paid" && line) {
            // Paid while cancelling: the payment request already marked the
            // line done, and Odoo resets its status once this answer arrives.
            setTimeout(() => line.setPaymentStatus("done"));
        }
        return false;
    }

    close() {
        if (this.creating) {
            this.creating.cancelled = true;
        }
        if (this.active && !this.active.stuck) {
            this._cancelActive();
        }
    }

    /**
     * Follow a payment line again after a reload of the till: the line still
     * shows a request in flight, but nothing in this tab is following it.
     */
    resumeLine(order, line) {
        const uuid = line.uuid;
        if (this.creating || this.active || this.refunding || this.resuming) {
            return; // one Tabby request at a time; this line is resumed once the till is free
        }
        // Claimed before the first await: a second mount of the payment
        // screen must not start another follow-up of the same line.
        this.resuming = uuid;
        this.pos.paymentTerminalInProgress = true;
        const done = line.amount < 0 ? this._refund(order, line) : this._resume(order, line);
        done.then((success) => {
            this.resuming = null;
            line.handlePaymentResponse(success);
            this.pos.paymentTerminalInProgress = false;
        });
    }

    async _resume(order, line) {
        line.setPaymentStatus("waitingCard");
        const res = await this._rpc("tabby_resume", this._lineData(order, line));
        if (res?.error) {
            // The server is unreachable: try again shortly, the line stays locked.
            await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
            return order.payment_ids.some((l) => l.uuid === line.uuid) ? this._resume(order, line) : false;
        }
        if (!res?.found || ["failed", "cancelled"].includes(res.state)) {
            return false; // nothing reached Tabby, or no money is held
        }
        return this._track(order, line, res);
    }

    // -- Refund (POS return) -------------------------------------------------

    async _refund(order, line) {
        const refundedLines = order.lines
            .filter((l) => Number.isInteger(l.refunded_orderline_id?.id))
            .map((l) => ({ id: l.refunded_orderline_id.id, qty: Math.abs(l.getQuantity()) }));
        if (!refundedLines.length) {
            this._showError(_t("Tabby refunds work from a return of a Tabby-paid order (Actions > Refund)."));
            return false;
        }
        // The server fixes the refund reference per payment line and keeps it
        // across retries: a retry after a lost response finds the same refund
        // in Tabby instead of refunding twice.
        this.refunding = line.uuid;
        let res;
        for (;;) {
            res = await this._rpc("tabby_refund", {
                ...this._lineData(order, line),
                amount: line.amount,
                refunded_lines: refundedLines,
            });
            if (!res?.uncertain) {
                break;
            }
            // The refund may have gone through: the line stays locked and the
            // same reference is retried until Tabby gives a definite answer.
            this._warnOnce(this._refundWarning, _t("Tabby did not confirm the refund yet. The till keeps checking; do not refund this amount another way."));
            await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
            if (!order.payment_ids.some((l) => l.uuid === line.uuid)) {
                // The line is gone: send no more refunds for it. The order
                // cannot be validated until the server knows the outcome.
                this.refunding = null;
                this._refundWarning.warned = false;
                this._showError(_t("The Tabby refund for this line is not confirmed. Check the payment in the Tabby merchant dashboard before refunding this amount another way."));
                return false;
            }
        }
        this.refunding = null;
        this._refundWarning.warned = false;
        if (!res || res.error) {
            this._showError(res?.error || _t("Tabby refund failed. Please try again."));
            return false;
        }
        line.transaction_id = res.refund_id || res.payment_id;
        line.setReceiptInfo(_t("Refunded with Tabby\nTabby payment ID:\n%s", res.payment_id));
        if (res.message) {
            this._showError(res.message); // refunded outside the POS: recorded, nothing sent
        }
        return true;
    }

    // -- Payment flow --------------------------------------------------------

    /** Follow the line's Tabby payment until Tabby gives an outcome. */
    _track(order, line, attempt) {
        line.transaction_id = attempt.payment_id;
        return new Promise((resolve) => {
            this.active = {
                seq: ++this._seq,
                uuid: line.uuid,
                line,
                order,
                payment_id: attempt.payment_id,
                resolve,
                deadline: Date.now() + this._timeoutMs(),
                timer: null,
                closeDialog: null,
                lastStatus: attempt.status || null,
                cancelRequested: false,
                warned: false,
                stuck: false, // waits for a decision in the Tabby dashboard
                outcome: null, // "paid" or "failed" once finished
            };
            if (attempt.state === "pending" && attempt.qr_image && attempt.status !== "AUTHORIZED") {
                this._openQr(line, order, attempt);
            }
            if (attempt.message) {
                this._stuck(this.active, attempt.message);
            }
            this._schedulePoll(attempt.resumed || attempt.found ? 0 : POLL_INTERVAL_MS);
        });
    }

    _openQr(line, order, attempt) {
        const active = this.active;
        line.qrPaymentData = {
            name: "Tabby",
            amount: this.env.utils.formatCurrency(line.amount),
            qrCode: attempt.qr_image,
        };
        active.closeDialog = this.env.services.dialog.add(QRPopup, {
            title: "Tabby",
            line,
            order,
            qrCode: attempt.qr_image,
            confirmLabel: _t("Check status"),
            cancelLabel: _t("Cancel payment"),
            confirm: async () => {
                await this._poll(true);
                return false; // keep the QR open while the payment is pending
            },
            cancel: async () => {
                await this._cancelActive(_t("Payment cancelled by the cashier."));
                return true; // an unresolved payment stays on its line, outside the dialog
            },
        });
    }

    _timeoutMs() {
        return Math.max(this.pos.config.tabby_timeout || DEFAULT_TIMEOUT_S, 180) * 1000;
    }

    _schedulePoll(delay) {
        const active = this.active;
        if (!active) {
            return;
        }
        clearTimeout(active.timer);
        const wait = delay ?? (active.stuck ? SLOW_POLL_INTERVAL_MS : POLL_INTERVAL_MS);
        active.timer = setTimeout(() => this._poll(false), wait);
    }

    async _poll(manual) {
        const active = this.active;
        if (!active) {
            return;
        }
        if (!manual && !active.stuck && Date.now() > active.deadline && !active.cancelRequested) {
            if (active.lastStatus === "AUTHORIZED") {
                // Paid but not captured yet: never fail or pass the sale on a guess.
                this._stuck(active, _t("The customer paid with Tabby, but the capture is not confirmed yet. Capture payment %s in the Tabby merchant dashboard; the till completes the sale once it sees the capture.", active.payment_id));
            } else {
                active.cancelRequested = true;
                await this._cancelActive(_t("The customer did not complete the Tabby payment in time."));
                return;
            }
        }
        const res = await this._rpc("tabby_payment_status", this._lineData(active.order, active.line));
        if (this.active?.seq !== active.seq) {
            return; // finished or cancelled meanwhile
        }
        if (res?.status) {
            active.lastStatus = res.status;
        }
        if (res?.outcome === "paid") {
            this._finish(true);
        } else if (res?.outcome === "failed") {
            this._finish(false, res.error || this._failureMessage(res.status));
        } else {
            if (res?.outcome === "partial") {
                this._stuck(active, this._partialMessage(active, res));
            }
            if (!manual) {
                this._schedulePoll(); // pending, partial or a transient error: keep checking
            }
        }
    }

    /**
     * Cancel the active session. Returns "cancelled", "paid", "failed" or
     * "unconfirmed". Only a confirmed outcome ends the payment: otherwise
     * (partial capture, authorized but not captured, no answer) the line
     * stays with its Tabby payment and the till keeps checking it.
     */
    async _cancelActive(message) {
        const active = this.active;
        if (!active) {
            return "cancelled";
        }
        clearTimeout(active.timer);
        const res = await this._rpc("tabby_cancel_session", this._lineData(active.order, active.line));
        if (this.active?.seq !== active.seq) {
            // Finished by a poll meanwhile: report what really happened.
            return active.outcome === "paid" ? "paid" : "cancelled";
        }
        if (res?.cancelled) {
            this._finish(false, message);
            return "cancelled";
        }
        if (res?.outcome === "paid") {
            this._finish(true);
            return "paid";
        }
        if (res?.outcome === "failed") {
            this._finish(false, this._failureMessage(res.status));
            return "failed";
        }
        this._stuck(
            active,
            res?.outcome === "partial"
                ? this._partialMessage(active, res)
                : _t("Tabby did not confirm the cancellation of payment %s. It stays on this order and the till keeps checking it; resolve it in the Tabby merchant dashboard if it does not clear.", active.payment_id)
        );
        this._schedulePoll();
        return "unconfirmed";
    }

    /** The payment needs a decision in the Tabby dashboard: free the screen, keep the line. */
    _stuck(active, message) {
        active.stuck = true;
        active.line.qrPaymentData = null;
        if (active.closeDialog) {
            active.closeDialog();
            active.closeDialog = null;
        }
        this._warnOnce(active, message);
    }

    _warnOnce(holder, message) {
        if (!holder.warned) {
            holder.warned = true;
            this._showError(message);
        }
    }

    _finish(success, message) {
        const active = this.active;
        if (!active) {
            return;
        }
        clearTimeout(active.timer);
        this.active = null;
        active.outcome = success ? "paid" : "failed";
        active.line.qrPaymentData = null;
        if (active.closeDialog) {
            active.closeDialog();
        }
        if (success) {
            active.line.setReceiptInfo(_t("Paid with Tabby\nTabby payment ID:\n%s", active.payment_id));
        }
        if (message) {
            this._showError(message);
        }
        active.resolve(success);
    }

    _partialMessage(active, res) {
        return _t(
            "Tabby closed payment %(id)s after collecting only %(captured)s. The line stays on this order: refund that amount in the Tabby merchant dashboard, and the till releases the line.",
            { id: active.payment_id, captured: res.captured }
        );
    }

    _failureMessage(status) {
        return status === "REJECTED"
            ? _t("Tabby could not approve this purchase. Please use another payment method.")
            : _t("The Tabby payment was cancelled or has expired. Send a new request or use another payment method.");
    }

    // -- Data ---------------------------------------------------------------

    _lineData(order, line) {
        return {
            config_id: this.pos.config.id,
            order_uuid: order.uuid,
            line_uuid: line.uuid,
            line_uuids: order.payment_ids.map((l) => l.uuid),
        };
    }

    _orderItems(order) {
        const decimals = order.currency?.decimal_places ?? this.pos.currency.decimal_places;
        return order.lines.map((l) => {
            const qty = l.getQuantity();
            const total = l.prices?.total_included ?? l.price_subtotal_incl; // tax included, after discounts
            const whole = Number.isInteger(qty) && qty > 0;
            return {
                // Tabby takes whole quantities: a weighed line goes as one item.
                title: whole ? l.getFullProductName() : `${l.getFullProductName()} (${qty})`,
                quantity: whole ? qty : 1,
                unit_price: (whole ? total / qty : total).toFixed(decimals),
                reference_id: l.product_id?.default_code || String(l.product_id?.id || ""),
                category: l.product_id?.pos_categ_ids?.[0]?.name || "POS",
            };
        });
    }

    _buyer(order) {
        const partner = order.getPartner();
        if (!partner) {
            return {};
        }
        return { name: partner.name, email: partner.email, phone: partner.phone };
    }

    _rpc(method, data) {
        return this.env.services.orm.silent
            .call("pos.payment.method", method, [[this.payment_method_id.id], data])
            .catch(() => ({
                error: _t("Could not reach the Odoo server. Check the connection and try again."),
                uncertain: true,
            }));
    }

    _showError(message) {
        this.env.services.dialog.add(AlertDialog, { title: _t("Tabby"), body: message });
    }
}
