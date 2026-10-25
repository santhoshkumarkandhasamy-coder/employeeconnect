import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../domain/payroll_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'payslip_view.dart';

/// One published payslip (employee view). Download PDF and Share both fetch the
/// bytes, keep them only in the private export folder and delete them after the
/// share sheet returns. Screenshot-protected. Nothing is cached on disk.
class PayslipDetailScreen extends ConsumerStatefulWidget {
  const PayslipDetailScreen({required this.id, super.key});
  final String id;

  @override
  ConsumerState<PayslipDetailScreen> createState() => _PayslipDetailScreenState();
}

class _PayslipDetailScreenState extends ConsumerState<PayslipDetailScreen> {
  bool _busy = false;

  Future<void> _fetchPdf() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await downloadPayrollFile(context, ref, (repo) => repo.myPayslipPdf(widget.id));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(myPayslipProvider(widget.id));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Payslip')),
        body: async.when(
          loading: () => const LoadingView(label: 'Loading payslip'),
          error: (e, _) => ErrorView(
            message: payrollMsg(e),
            onRetry: e is ApiException && e.code == 'NOT_FOUND'
                ? null
                : () => ref.invalidate(myPayslipProvider(widget.id)),
          ),
          data: (p) => ListView(
            padding: const EdgeInsets.all(16),
            children: [
              if (p.revisionInfo != null || p.isSuperseded) _RevisionBanner(payslip: p),
              PayslipView(payslip: p),
              const SizedBox(height: 12),
              FilledButton.icon(
                key: const ValueKey('download-pdf'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _busy ? null : _fetchPdf,
                icon: _busy
                    ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.download_outlined),
                label: const Text('Download PDF'),
              ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                key: const ValueKey('share-pdf'),
                style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _busy ? null : _fetchPdf,
                icon: const Icon(Icons.share_outlined),
                label: const Text('Share'),
              ),
              const SizedBox(height: 12),
              Text(
                'The PDF is kept only in private app storage and is deleted right after '
                'sharing. Share it only with people you trust.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RevisionBanner extends StatelessWidget {
  const _RevisionBanner({required this.payslip});
  final Payslip payslip;

  void _open(BuildContext context, String id) => Navigator.of(context).push(
        MaterialPageRoute<void>(builder: (_) => PayslipDetailScreen(id: id)),
      );

  @override
  Widget build(BuildContext context) {
    final info = payslip.revisionInfo;
    final newer = info?.supersededById;
    final older = info?.supersedesId;
    final cs = Theme.of(context).colorScheme;
    return Card(
      color: cs.tertiaryContainer,
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              payslip.isSuperseded
                  ? 'This payslip was replaced by a newer revision.'
                  : 'Revised payslip (revision ${payslip.revision}).',
              style: TextStyle(fontWeight: FontWeight.w600, color: cs.onTertiaryContainer),
            ),
            if (info?.reason != null)
              Text('Reason: ${info!.reason}', style: TextStyle(color: cs.onTertiaryContainer)),
            Wrap(
              spacing: 8,
              children: [
                if (newer != null)
                  TextButton(
                    onPressed: () => _open(context, newer),
                    child: const Text('View the latest revision'),
                  ),
                if (older != null)
                  TextButton(
                    onPressed: () => _open(context, older),
                    child: const Text('View the earlier revision'),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
