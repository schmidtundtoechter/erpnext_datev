# Copyright (c) 2021, ALYF GmbH and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.translate import print_language


class DATEVUnternehmenOnlineSettings(Document):
	def validate(self):
		for voucher_config in self.datev_voucher_config:
			if not voucher_config.attach_print and not voucher_config.attach_files:
				frappe.throw(
					_("Please configure attachments for voucher type {}.").format(
						_(voucher_config.voucher_type)
					)
				)


def send(doc, method):
	settings = frappe.get_single("DATEV Unternehmen Online Settings")
	if not settings.enabled:
		return

	if not get_voucher_config(settings, doc.doctype):
		return

	frappe.enqueue(
		"erpnext_datev.erpnext_datev.doctype.datev_unternehmen_online_settings"
		".datev_unternehmen_online_settings.do_send",
		doctype=doc.doctype,
		docname=doc.name,
		enqueue_after_commit=True,
	)


def do_send(doctype, docname):
	doc = frappe.get_doc(doctype, docname)
	settings = frappe.get_single("DATEV Unternehmen Online Settings")
	if not settings.enabled:
		return

	voucher_config = get_voucher_config(settings, doc.doctype)
	if not voucher_config:
		return

	attachments = []

	if voucher_config.attach_print:
		document_language = doc.language if hasattr(doc, "language") else None
		language = document_language or settings.default_print_language or frappe.db.get_default("lang")
		filename = attach_print(
			doc.doctype,
			doc.name,
			language,
			voucher_config.print_format,
		)
		attachments.append(filename)

	if voucher_config.attach_files:
		file_names = get_attached_files(doc.doctype, doc.name)

		if not file_names:
			# pdf_a_3 runs synchronously in on_submit so its File record is committed
			# before this background job starts. Fall back directly to disk if the DB
			# record is somehow missing.
			att = _read_pdfa_from_disk(doc.name)
			if att:
				attachments.append(att)
		else:
			for file_name in file_names:
				att = _read_file_content(file_name)
				if att:
					attachments.append(att)
				else:
					frappe.log_error(
						title=_("DATEV: could not read file {} for {} {}").format(file_name, doctype, docname),
						reference_doctype=doctype,
						reference_name=docname,
					)

	if not attachments:
		frappe.log_error(
			title=_("{} was not sent to DATEV because no attachments have been found.").format(_(doc.doctype)),
			reference_doctype=doc.doctype,
			reference_name=doc.name,
		)
		return

	from frappe.core.doctype.communication.email import _make
	try:
		_make(
			doctype=doc.doctype,
			name=doc.name,
			content=_("New {0} {1} sent by the ERPNext-DATEV integration.").format(_(doc.doctype), doc.name),
			subject=f"{_(doc.doctype)}: {doc.name}",
			sender=frappe.get_value("Email Account", settings.sender, "email_id"),
			recipients=[voucher_config.recipient],
			communication_medium="Email",
			send_email=True,
			attachments=attachments,
			communication_type="Automated Message",
			add_signature=False,
		)
	except Exception:
		frappe.log_error(
			title=_("DATEV: failed to send email for {} {}").format(doctype, docname),
			message=frappe.get_traceback(),
			reference_doctype=doctype,
			reference_name=docname,
		)


def attach_print(doctype, name, language, print_format):
	with print_language(language):
		data = frappe.get_print(doctype, name, print_format, as_pdf=True)

	if doctype == "Sales Invoice" and "eu_einvoice" in frappe.get_installed_apps():
		try:
			from eu_einvoice.european_e_invoice.custom.sales_invoice import attach_xml_to_pdf

			data = attach_xml_to_pdf(name, data)
		except Exception:
			msg = _("Failed to attach XML to Sales Invoice PDF for DATEV")
			frappe.log_error(title=msg, reference_doctype=doctype, reference_name=name)
			frappe.msgprint(msg, indicator="red", alert=True)

	file = frappe.new_doc("File")
	file.file_name = f"{name}.pdf"
	file.content = data
	file.attached_to_doctype = doctype
	file.attached_to_name = name
	file.is_private = 1
	file.save()

	return file.name


def get_voucher_config(settings: DATEVUnternehmenOnlineSettings, doctype: str):
	voucher_config = settings.get("datev_voucher_config", filters={"voucher_type": doctype})
	if not voucher_config:
		return

	return voucher_config[0]


def get_attached_files(doctype: str, docname: str):
	# Raw SQL bypasses all Frappe permission and filtering layers.
	rows = frappe.db.sql(
		"SELECT name FROM `tabFile` WHERE attached_to_doctype=%s AND attached_to_name=%s",
		(doctype, docname),
	)
	return [r[0] for r in rows]


def _read_file_content(file_name: str) -> "dict | None":
	"""Read a File document's bytes directly from disk, bypassing all permission checks."""
	file_info = frappe.db.get_value(
		"File", file_name, ["file_name", "file_url", "is_private"], as_dict=True
	)
	if not file_info or not file_info.file_url:
		return None

	url = file_info.file_url
	if url.startswith("/private/"):
		full_path = frappe.get_site_path() + url
	elif url.startswith("/files/"):
		full_path = frappe.get_site_path("public") + url
	else:
		return None

	try:
		with open(full_path, "rb") as f:
			return {"fname": file_info.file_name, "fcontent": f.read()}
	except OSError:
		frappe.log_error(
			title=f"DATEV: cannot read file {file_name} from {full_path}",
			message=frappe.get_traceback(),
		)
		return None


def _read_pdfa_from_disk(docname: str) -> "dict | None":
	"""Read the PDF/A-3 file from disk using pdf_a_3's naming convention.

	pdf_a_3 names its output '{docname}.pdf' and stores it as a private file at
	'<site>/private/files/{docname}.pdf'.  Reading it here matches exactly what
	pdf_a_3's forward_pdf_to_archive() does — avoiding any DB dependency.
	"""
	import os

	file_name = f"{docname}.pdf".replace("/", "-")
	file_path = frappe.get_site_path("private", "files", file_name)

	if not os.path.isfile(file_path):
		frappe.log_error(
			title=f"DATEV: pdf_a_3 file not found on disk for {docname} ({file_path})",
		)
		return None

	try:
		with open(file_path, "rb") as f:
			return {"fname": file_name, "fcontent": f.read()}
	except OSError:
		frappe.log_error(
			title=f"DATEV: cannot read PDF from disk: {file_path}",
			message=frappe.get_traceback(),
		)
		return None
