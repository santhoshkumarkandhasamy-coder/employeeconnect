import 'package:flutter/material.dart';

import '../../../core/api/api_exception.dart';
import '../../admin/domain/admin_models.dart' show adminErrorMessage;
import '../../admin/presentation/admin_widgets.dart' show showAdminSnack;
import '../data/device_repository.dart';
import '../domain/device_models.dart';

/// Starts a background job (test or sync) and follows it until it finishes
/// (poll every 2 s, give up after 2 minutes). Failures are explained in plain
/// words in a snack bar; returns the finished job or null when it could not run.
Future<DeviceSync?> runDeviceJob(
  BuildContext context,
  DeviceRepository repo,
  Future<String> Function() start, {
  Future<void> Function(Duration)? wait,
}) async {
  try {
    final id = await start();
    final job = await repo.waitForSync(id, wait: wait, cancelled: () => !context.mounted);
    return job;
  } on ApiException catch (e) {
    if (context.mounted) showAdminSnack(context, adminErrorMessage(e));
  } on SyncPollTimeout {
    if (context.mounted) {
      showAdminSnack(context, 'The machine is taking longer than expected. Check the device again in a few minutes.');
    }
  }
  return null;
}

/// Details of a finished test, or the plain-language error.
class TestResultView extends StatelessWidget {
  const TestResultView({required this.job, super.key});
  final DeviceSync job;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (!job.succeeded) {
      return Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.error_outline, color: theme.colorScheme.error),
          const SizedBox(width: 8),
          Expanded(child: Text(job.summary, key: const ValueKey('test-error'))),
        ],
      );
    }
    final r = job.result;
    final rows = <(String, String)>[
      ('Serial number', r?.serialNumber ?? '-'),
      ('Model', r?.model ?? '-'),
      ('Firmware', r?.firmware ?? '-'),
      ('Machine time', r?.deviceTime?.replaceFirst('T', ' ') ?? '-'),
      ('Clock difference', clockOffsetText(r?.clockOffsetS)),
      ('People on the machine', '${r?.usersOnDevice ?? '-'}'),
      ('Records on the machine', '${r?.recordsOnDevice ?? '-'}'),
    ];
    return Column(
      key: const ValueKey('test-ok'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(children: [
          Icon(Icons.check_circle_outline, color: theme.colorScheme.primary),
          const SizedBox(width: 8),
          const Text('Connection works.'),
        ],),
        const SizedBox(height: 8),
        for (final (k, v) in rows)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 2),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(width: 160, child: Text(k, style: theme.textTheme.bodySmall)),
                Expanded(child: Text(v)),
              ],
            ),
          ),
      ],
    );
  }
}
