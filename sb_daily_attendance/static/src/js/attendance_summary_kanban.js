/** @odoo-module **/

import { registry } from "@web/core/registry";
import { kanbanView } from "@web/views/kanban/kanban_view";
import { KanbanRenderer } from "@web/views/kanban/kanban_renderer";
import { useService } from "@web/core/utils/hooks";

/** Read-only totals derived from the same records as the visible team cards. */
class AttendanceSummaryRenderer extends KanbanRenderer {
    static template = "sb_daily_attendance.AttendanceSummaryRenderer";

    setup() {
        super.setup();
        this.actionService = useService("action");
        this.notification = useService("notification");
        const date = this.props.list?.records?.[0]?.data?.dashboard_date;
        const today = new Date();
        const localToday = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
        this.dashboardState = {date: this.normaliseDate(date) || localToday};
    }

    normaliseDate(value) {
        // Odoo Date fields can arrive as ISO strings or Luxon DateTime objects.
        if (!value) return null;
        if (typeof value === "string") {
            const match = /^(\d{4}-\d{2}-\d{2})/.exec(value);
            return match ? match[1] : null;
        }
        if (typeof value.toISODate === "function") return value.toISODate();
        if (typeof value.toISO === "function") return value.toISO()?.slice(0, 10) || null;
        return null;
    }

    get selectedDate() {
        // Prefer the server-computed date (also used for the Kanban cards).
        // Keep the controlled input's value strictly YYYY-MM-DD.
        return this.normaliseDate(this.props.list?.records?.[0]?.data?.dashboard_date)
            || this.dashboardState.date;
    }

    get selectedDateHeading() {
        const parts = this.selectedDate?.split("-").map(Number);
        if (!parts || parts.length !== 3 || parts.some(n => !Number.isFinite(n))) return "Selected attendance date";
        const date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
        return `Attendance for ${new Intl.DateTimeFormat("en-ZA", {
            weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
        }).format(date)}`;
    }

    async changeDashboardDate(ev) {
        const date = ev.target.value;
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
        await this.navigateToDate(date);
    }

    async shiftDashboardDate(days) {
        const parts = this.selectedDate.split("-").map(Number);
        const d = new Date(Date.UTC(parts[0], parts[1]-1, parts[2]+days));
        await this.navigateToDate(d.toISOString().slice(0,10));
    }

    async navigateToDate(date) {
        this.dashboardState.date = date;
        await this.actionService.doAction("sb_daily_attendance.action_attendance_dashboard", {
            additionalContext: {sb_dashboard_date: date},
        });
    }

    async showToday() {
        const now = new Date();
        await this.navigateToDate(`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")}`);
    }

    get isAfterMorningCutoff() {
        // Cutoff applies to the current South African date only, never historical dates.
        const parts = new Intl.DateTimeFormat("en-CA", {
            timeZone: "Africa/Johannesburg", year: "numeric", month: "2-digit",
            day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
        }).formatToParts(new Date());
        const value = (name) => parts.find((p) => p.type === name)?.value;
        const today = `${value("year")}-${value("month")}-${value("day")}`;
        const minutes = Number(value("hour")) * 60 + Number(value("minute"));
        return this.selectedDate === today && minutes >= 10 * 60;
    }

    get whatsappSummary() {
        const t = this.attendanceTotals;
        const heading = this.selectedDateHeading.replace(/^Attendance for /, "");
        const generated = new Intl.DateTimeFormat("en-GB", {
            timeZone: "Africa/Johannesburg", day: "2-digit", month: "long", year: "numeric",
            hour: "2-digit", minute: "2-digit", hourCycle: "h23",
        }).format(new Date());
        const lines = [
            "⚡ *SAMBROS | MORNING ATTENDANCE*",
            `*${heading}*`,
            `_Generated: ${generated} (SAST)_`,
            "",
            "*TEAM ATTENDANCE*",
        ];
        const rows = this.props.list?.records || [];
        for (const row of rows) {
            const d = row.data || {};
            const name = String(d.name || "Unnamed team").replace(/[*_~`]/g, "");
            const status = d.dashboard_status;
            if (status === "missing") {
                lines.push(`🔴 *${name}:* Register outstanding`);
            } else if (status === "not_scheduled") {
                lines.push(`⚪ *${name}:* Not scheduled`);
            } else {
                const present = Number(d.dashboard_present || 0);
                const total = Number(d.dashboard_team_size || 0);
                const absent = Number(d.dashboard_absent || 0);
                const prevented = Number(d.dashboard_prevented || 0);
                const statusInfo = {
                    final: ["🟢", "Finalised"], submitted: ["🔵", "Submitted"],
                    closing: ["🟠", "Closing"], draft: ["🟠", "Open"],
                }[status] || ["🟠", "In progress"];
                lines.push(`${statusInfo[0]} *${name}:* *${present}/${total} present* — ${statusInfo[1]}`);
                if (absent || prevented) lines.push(`   Absent: *${absent}* | Prevented: *${prevented}*`);
            }
        }
        lines.push("", "*MANAGEMENT SUMMARY*",
            `Teams: *${t.teams}*`,
            `Present: *${t.present}* | Absent: *${t.absent}* | Prevented: *${t.prevented}*`,
            `${t.outstanding ? "🔴 " : "🟢 "}Outstanding registers: *${t.outstanding}*`,
            `🟣 Approved overtime: *${t.approved.toFixed(2)} hrs*`,
            `${t.pending > 0 ? "🟠 " : ""}Pending overtime: *${t.pending.toFixed(2)} hrs*`
        );
        // Missing registers mean company attendance totals are provisional.
        if (t.outstanding > 0) {
            lines.push("", "⚠️ *ATTENDANCE INCOMPLETE*",
                `${t.outstanding} team register${t.outstanding === 1 ? " is" : "s are"} outstanding. Company totals may change.`);
        }
        lines.push("", "Please update outstanding registers and review exceptions in Odoo.",
            "_Attendance and overtime approvals remain recorded in Odoo._");
        return lines.join("\n");
    }

    async copyWhatsappSummary() {
        try {
            await navigator.clipboard.writeText(this.whatsappSummary);
            this.notification.add("Attendance summary copied. Review it before sharing in the HR WhatsApp group.", {type: "success"});
        } catch (error) {
            this.notification.add("Could not copy automatically. Please use a secure browser connection and try again.", {type: "warning"});
        }
    }

    get attendanceTotals() {
        const rows = this.props.list?.records || [];
        const totals = { teams: rows.length, present: 0, absent: 0, prevented: 0, approved: 0, pending: 0, outstanding: 0 };
        for (const row of rows) {
            const d = row.data || {};
            totals.present += Number(d.dashboard_present || 0);
            totals.absent += Number(d.dashboard_absent || 0);
            totals.prevented += Number(d.dashboard_prevented || 0);
            totals.approved += Number(d.dashboard_approved_ot || 0);
            totals.pending += Number(d.dashboard_pending_ot || 0);
            if (d.dashboard_status === "missing") totals.outstanding += 1;
        }
        return totals;
    }
}

registry.category("views").add("sb_attendance_summary_kanban", {
    ...kanbanView,
    Renderer: AttendanceSummaryRenderer,
});
