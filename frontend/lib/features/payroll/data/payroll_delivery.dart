import 'dart:io';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/files/share_confirm.dart';
import '../../food/data/food_repository.dart'
    show ExportStore, FileOpener, OpenResult, exportStoreProvider, fileOpenerProvider, shareConfirmedProvider;
import 'payroll_repository.dart';

enum DeliveryOutcome { done, cancelled, unavailable }

/// Hands a downloaded payslip PDF / register workbook to the user.
///
/// Mobile: [StoreFileDelivery] (private `ExportStore` folder, then the share
/// sheet, then the file is deleted). The admin console overrides the provider
/// with a browser blob download.
abstract class PayrollFileDelivery {
  Future<DeliveryOutcome> deliver(BuildContext context, PayrollFile file);
}

/// Saves into the app-private [ExportStore], opens the share sheet through
/// [FileOpener] and deletes the file again whatever happens. The folder is
/// also wiped on logout, lock and next start (see `exportStoreProvider`).
class StoreFileDelivery implements PayrollFileDelivery {
  StoreFileDelivery({required this.store, required this.opener, required this.confirmed});
  final ExportStore store;
  final FileOpener opener;
  final StateController<bool> confirmed;

  @override
  Future<DeliveryOutcome> deliver(BuildContext context, PayrollFile file) async {
    if (!await confirmShareOnce(context, confirmed)) return DeliveryOutcome.cancelled;
    final saved = await store.save(file.bytes, file.filename);
    try {
      final r = await opener.open(saved.path);
      return switch (r) {
        OpenResult.handedOver => DeliveryOutcome.done,
        OpenResult.dismissed => DeliveryOutcome.cancelled,
        OpenResult.unavailable => DeliveryOutcome.unavailable,
      };
    } finally {
      _deleteQuietly(saved);
    }
  }

  static void _deleteQuietly(File f) {
    try {
      if (f.existsSync()) f.deleteSync();
    } on Object {
      // The folder is wiped on logout, lock and next start anyway.
    }
  }
}

final payrollDeliveryProvider = Provider<PayrollFileDelivery>(
  (ref) => StoreFileDelivery(
    store: ref.watch(exportStoreProvider),
    opener: ref.watch(fileOpenerProvider),
    confirmed: ref.read(shareConfirmedProvider.notifier),
  ),
);
