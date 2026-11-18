import { _t } from "@web/core/l10n/translation";
import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { QRPopup } from "@point_of_sale/app/utils/qr_code_popup/qr_code_popup";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

// Tabby POS flow: https://docs.tabby.ai/offline-payment-methods/pos-integration
const POLL_INTERVAL_MS = 5000; // the docs ask for 5 s
const POLL_TIMEOUT_MS = 300000; // 300 s, never below 180 s
const MAX_UNCONFIRMED_CANCELS = 5;
// Returned by send_payment_cancel when the payment must not be dropped
// (paid, or the cancel is not confirmed): Odoo removes or resets the line
// once this promise resolves, so it never does; the payment request itself
// resolves the line when the final status is known.
const KEEP_LINE = () => new Promise(() => {});

export class PaymentTabby extends PaymentInterface {
    setup() {
        super.setup(...arguments);
        this.creating = null; // {uuid, cancelled} while the session is being created
        this.active = null; // {uuid, line, session_id, payment_id, resolve, timer, deadline, closeDialog, lastStatus, cancelFailures}
    }

    get fast_payments() {
        return false; // let the cashier adjust the amount before sending
    }

    async send_payment_request(uuid) {
        super.send_payment_request(uuid);
        const order = this.pos.get_order();
        const line = order.payment_ids.find((l) => l.uuid === uuid) || order.get_selected_paymentline();
        if (!line || !line.amount) {
            return false;
        }
        if (line.amount < 0) {
            return this._refund(order, line);
        }
        line.set_payment_status("waitingCard");

        const attempt = { uuid: line.uuid, cancelled: false };
        this.creating = attempt;
        const res = await this._rpc("tabby_create_session", {
            config_id: this.pos.config.id,
            amount: line.amount,
            order_uuid: order.uuid,
            order_reference: order.name || order.uuid,
            items: this._orderItems(order),
            buyer: this._buyer(order),
        });
        this.creating = null;
        if (attempt.cancelled) {
            // The cashier cancelled while the session was being created.
            if (res && res.session_id) {
                this._rpc("tabby_cancel_session", {
                    config_id: this.pos.config.id,
                    session_id: res.session_id,
                    payment_id: res.payment_id,
                });
            }
            return false;
        }
        if (!res || res.error) {
            this._showError(res?.error || _t("Tabby is unavailable. Please use another payment method."));
            return false;
        }

        line.transaction_id = res.payment_id;
        line.qrPaymentData = {
            name: "Tabby",
            amount: this.env.utils.formatCurrency(line.amount),
            qrCode: res.qr_image,
        };
        return this._waitForResult(line, order, res);
    }

    async send_payment_cancel(order, uuid) {
        super.send_payment_cancel(order, uuid);
        if (this.creating && this.creating.uuid === uuid) {
            this.creating.cancelled = true;
            return true;
        }
        if (this.active && this.active.uuid === uuid) {
            const result = await this._cancelActive(_t("Payment cancelled by the cashier."));
            return result === "cancelled" ? true : KEEP_LINE();
        }
        return true;
    }

    close() {
        if (this.creating) {
            this.creating.cancelled = true;
        }
        if (this.active) {
            this._cancelActive();
        }
    }

    // -- Refund (POS return) -------------------------------------------------

    async _refund(order, line) {
        const refundedLineIds = order.lines
            .map((l) => l.refunded_orderline_id?.id)
            .filter((id) => Number.isInteger(id));
        if (!refundedLineIds.length) {
            this._showError(_t("Tabby refunds work from a return of a Tabby-paid order (Actions > Refund)."));
            return false;
        }
        const res = await this._rpc("tabby_refund", {
            config_id: this.pos.config.id,
            refunded_line_ids: refundedLineIds,
            amount: line.amount,
            // Stable per return order: a retry after a lost response finds the
            // same refund in Tabby instead of refunding twice.
            reference_id: order.uuid,
        });
        if (!res || res.error) {
            this._showError(res?.error || _t("Tabby refund failed. Please try again."));
            return false;
        }
        line.transaction_id = res.refund_id || res.payment_id;
        line.set_receipt_info(_t("Refunded with Tabby\nTabby payment ID:\n%s", res.payment_id));
        return true;
    }

    // -- Payment flow --------------------------------------------------------

    _waitForResult(line, order, session) {
        return new Promise((resolve) => {
            this.active = {
                uuid: line.uuid,
                line,
                session_id: session.session_id,
                payment_id: session.payment_id,
                web_url: session.web_url,
                resolve,
                deadline: Date.now() + POLL_TIMEOUT_MS,
                timer: null,
                closeDialog: null,
                lastStatus: null,
                cancelFailures: 0,
            };
            this.active.closeDialog = this.env.services.dialog.add(QRPopup, {
                title: "Tabby",
                line,
                order,
                qrCode: session.qr_image,
                confirmLabel: _t("Check status"),
                cancelLabel: _t("Cancel payment"),
                confirm: async () => {
                    await this._poll(true);
                    return false; // keep the QR open while the payment is pending
                },
                cancel: async () => {
                    const result = await this._cancelActive(_t("Payment cancelled by the cashier."));
                    return result !== "unconfirmed"; // keep the QR open if Tabby did not confirm
                },
            });
            this._schedulePoll();
        });
    }

    _schedulePoll() {
        if (!this.active) {
            return;
        }
        clearTimeout(this.active.timer);
        this.active.timer = setTimeout(() => this._poll(false), POLL_INTERVAL_MS);
    }

    async _poll(manual) {
        const active = this.active;
        if (!active) {
            return;
        }
        if (!manual && Date.now() > active.deadline) {
            if (active.lastStatus === "AUTHORIZED") {
                // The customer paid but the capture keeps failing: the sale stands.
                this._finish(
                    true,
                    _t("The customer paid with Tabby, but the capture was not confirmed. Capture payment %s in the Tabby merchant dashboard.", active.payment_id)
                );
                return;
            }
            await this._cancelActive(_t("The customer did not complete the Tabby payment in time."));
            return;
        }
        const res = await this._rpc("tabby_payment_status", {
            config_id: this.pos.config.id,
            payment_id: active.payment_id,
        });
        if (this.active !== active) {
            return; // finished or cancelled meanwhile
        }
        if (res?.status) {
            active.lastStatus = res.status;
        }
        if (res?.outcome === "paid") {
            this._finish(true);
        } else if (res?.outcome === "failed") {
            this._finish(false, this._failureMessage(res.status));
        } else if (!manual) {
            this._schedulePoll(); // pending or a transient error: keep polling until the deadline
        }
    }

    /**
     * Cancel the active session. Returns "cancelled", "paid", "failed" or
     * "unconfirmed". Only a confirmed outcome ends the payment: when Tabby
     * does not confirm, the till keeps polling so a payment the customer
     * completes meanwhile is not lost.
     */
    async _cancelActive(message) {
        const active = this.active;
        if (!active) {
            return "cancelled";
        }
        clearTimeout(active.timer);
        const res = await this._rpc("tabby_cancel_session", {
            config_id: this.pos.config.id,
            session_id: active.session_id,
            payment_id: active.payment_id,
        });
        if (this.active !== active) {
            return "cancelled";
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
        active.cancelFailures += 1;
        if (active.cancelFailures >= MAX_UNCONFIRMED_CANCELS) {
            this._finish(
                false,
                _t("Tabby did not confirm the cancellation. Check payment %s in the Tabby merchant dashboard before taking another payment.", active.payment_id)
            );
            return "failed";
        }
        this._showError(_t("Tabby did not confirm the cancellation yet. The till keeps checking the payment."));
        this._schedulePoll();
        return "unconfirmed";
    }

    _finish(success, message) {
        const active = this.active;
        if (!active) {
            return;
        }
        clearTimeout(active.timer);
        this.active = null;
        active.line.qrPaymentData = null;
        if (active.closeDialog) {
            active.closeDialog();
        }
        if (success) {
            active.line.set_receipt_info(_t("Paid with Tabby\nTabby payment ID:\n%s", active.payment_id));
        }
        if (message) {
            this._showError(message);
        }
        active.resolve(success);
    }

    _failureMessage(status) {
        return status === "REJECTED"
            ? _t("Tabby could not approve this purchase. Please use another payment method.")
            : _t("The Tabby payment was cancelled or has expired. Send a new request or use another payment method.");
    }

    // -- Data ---------------------------------------------------------------

    _orderItems(order) {
        return order.lines.map((l) => ({
            title: l.get_full_product_name ? l.get_full_product_name() : l.product_id?.display_name,
            quantity: l.get_quantity ? l.get_quantity() : l.qty,
            unit_price: String(l.get_unit_display_price ? l.get_unit_display_price() : l.price_unit),
            reference_id: l.product_id?.default_code || String(l.product_id?.id || ""),
            category: l.product_id?.pos_categ_ids?.[0]?.name || "POS",
        }));
    }

    _buyer(order) {
        const partner = order.get_partner ? order.get_partner() : order.partner_id;
        if (!partner) {
            return {};
        }
        return { name: partner.name, email: partner.email, phone: partner.mobile || partner.phone };
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
