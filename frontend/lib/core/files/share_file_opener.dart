import 'dart:async';

import 'package:share_plus/share_plus.dart';

import '../../features/food/data/food_repository.dart' show ExportStore, FileOpener, OpenResult;

/// Maps share_plus' result to [OpenResult]. `unavailable` from share_plus means
/// "the sheet was shown but the platform cannot say what happened", so it counts
/// as handed over; only an explicit `dismissed` is treated as cancelled.
OpenResult openResultFor(ShareResultStatus status) =>
    status == ShareResultStatus.dismissed ? OpenResult.dismissed : OpenResult.handedOver;

/// Hands a private file to the OS share sheet (save to Drive, e-mail, open in
/// a spreadsheet app). `main.dart` overrides `fileOpenerProvider` with this.
///
/// share_plus copies the file to `<cache>/share_plus`; that copy is deleted by
/// [store] shortly after the sheet returns (receiving apps normally read it
/// right away; a grace period avoids breaking a slow upload) and again at start,
/// logout and lock. Review 08 M1.
class ShareFileOpener implements FileOpener {
  const ShareFileOpener({
    this.store,
    this.copyGrace = const Duration(minutes: 2),
    this.share,
  });

  final ExportStore? store;
  final Duration copyGrace;

  /// Test seam; defaults to `Share.shareXFiles`.
  final Future<ShareResult> Function(List<XFile> files)? share;

  @override
  Future<OpenResult> open(String path) async {
    try {
      final run = share ?? (files) => Share.shareXFiles(files);
      final result = await run([XFile(path)]);
      final outcome = openResultFor(result.status);
      final s = store;
      if (s != null) {
        if (outcome == OpenResult.dismissed) {
          unawaited(s.clearShareCopies());
        } else {
          unawaited(Future<void>.delayed(copyGrace, s.clearShareCopies));
        }
      }
      return outcome;
    } on Object {
      return OpenResult.unavailable;
    }
  }
}
