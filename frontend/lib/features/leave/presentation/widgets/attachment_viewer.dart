import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../core/api/api_exception.dart';
import '../../../../core/files/file_service.dart';
import '../../../food/data/food_repository.dart'
    show OpenResult, exportStoreProvider, fileOpenerProvider;
import '../../domain/leave_logic.dart';
import '../../domain/leave_models.dart';

/// Text for a failed document fetch (404 = removed by retention or no access).
String attachmentErrorMessage(Object e) {
  if (e is ApiException) {
    if (e.code == 'NOT_FOUND' || e.status == 404) {
      return 'This document is no longer available. It may have been removed or is not yours to view.';
    }
    if (e.code == 'FORBIDDEN') return 'You are not allowed to view this document.';
    return e.message;
  }
  return 'Could not open the document.';
}

/// "View document" button. The file is fetched through the authenticated Dio
/// client into memory (no token in any URL). Images are shown in a dialog;
/// PDFs are saved to the app-private folder and handed to the file opener, the
/// same pattern as announcement PDFs and the food export.
class AttachmentButton extends ConsumerStatefulWidget {
  const AttachmentButton({required this.item, super.key});
  final RequestItem item;

  @override
  ConsumerState<AttachmentButton> createState() => _AttachmentButtonState();
}

class _AttachmentButtonState extends ConsumerState<AttachmentButton> {
  bool _busy = false;

  Future<void> _open() async {
    final item = widget.item;
    final url = item.attachmentUrl;
    if (url == null || _busy) return;
    final messenger = ScaffoldMessenger.of(context);
    final files = ref.read(fileServiceProvider);
    final store = ref.read(exportStoreProvider);
    final opener = ref.read(fileOpenerProvider);
    setState(() => _busy = true);
    try {
      final bytes = await files.bytes(url);
      final type = sniffContentType(bytes, declared: item.attachmentContentType);
      if (type == null) {
        messenger.showSnackBar(const SnackBar(content: Text('This document type cannot be shown.')));
        return;
      }
      if (type == 'application/pdf') {
        final id = item.id.replaceAll(RegExp(r'[^A-Za-z0-9-]'), '_');
        final file = await store.save(bytes, 'leave-$id.pdf');
        final opened = await opener.open(file.path);
        if (!mounted || opened != OpenResult.unavailable) return;
        await showDialog<void>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: const Text('PDF saved'),
            content: Text('The PDF was saved in the app\'s private storage:\n\n${file.path}\n\n'
                'It is deleted when you log out or the app locks.'),
            actions: [
              TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Close')),
            ],
          ),
        );
      } else {
        if (!mounted) return;
        await showDialog<void>(
          context: context,
          builder: (ctx) => Dialog(
            child: Stack(
              children: [
                InteractiveViewer(
                  child: Image.memory(
                    bytes,
                    fit: BoxFit.contain,
                    semanticLabel: 'Leave supporting document',
                    errorBuilder: (_, __, ___) =>
                        const Padding(padding: EdgeInsets.all(24), child: Text('The image could not be shown.')),
                  ),
                ),
                Positioned(
                  right: 0,
                  top: 0,
                  child: IconButton(
                    tooltip: 'Close',
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.pop(ctx),
                  ),
                ),
              ],
            ),
          ),
        );
      }
    } on Object catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(attachmentErrorMessage(e))));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Align(
        alignment: Alignment.centerLeft,
        child: OutlinedButton.icon(
          onPressed: _busy ? null : _open,
          icon: _busy
              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.attach_file),
          label: const Text('View document'),
        ),
      );
}
