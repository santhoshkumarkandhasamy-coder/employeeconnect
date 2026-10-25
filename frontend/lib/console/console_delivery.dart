import 'package:flutter/widgets.dart';

import '../features/payroll/data/payroll_delivery.dart';
import '../features/payroll/data/payroll_repository.dart';
import 'web_download.dart';

/// Console delivery of payslip PDFs and the register workbook: a browser blob
/// download (no share sheet, no files on a device we control).
class BrowserFileDelivery implements PayrollFileDelivery {
  const BrowserFileDelivery();

  static String mimeFor(String filename) => filename.toLowerCase().endsWith('.xlsx')
      ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
      : 'application/pdf';

  @override
  Future<DeliveryOutcome> deliver(BuildContext context, PayrollFile file) async {
    try {
      await saveBlob(file.bytes, file.filename, mimeFor(file.filename));
      return DeliveryOutcome.done;
    } on Object {
      return DeliveryOutcome.unavailable;
    }
  }
}
