import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import '../domain/ops_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

/// Banner for a grace / expired / invalid license; empty when all is well.
class LicenseBanner extends StatelessWidget {
  const LicenseBanner({required this.license, super.key});
  final LicenseInfo license;

  @override
  Widget build(BuildContext context) {
    final text = licenseBanner(license);
    if (text == null) return const SizedBox.shrink();
    return MaterialBanner(
      content: Text(text),
      leading: const Icon(Icons.warning_amber_rounded),
      actions: const [SizedBox.shrink()],
    );
  }
}

/// Admin > License (`license.manage`): seats, status, expiry, deployment id
/// (copy for the vendor) and the install form (step-up password).
class LicenseScreen extends ConsumerStatefulWidget {
  const LicenseScreen({super.key});

  @override
  ConsumerState<LicenseScreen> createState() => _LicenseScreenState();
}

class _LicenseScreenState extends ConsumerState<LicenseScreen> {
  final _key = GlobalKey<FormState>();
  final _license = TextEditingController();
  bool _busy = false;

  @override
  void dispose() {
    _license.dispose();
    super.dispose();
  }

  Future<void> _install() async {
    if (_busy || !(_key.currentState?.validate() ?? false)) return;
    setState(() => _busy = true);
    try {
      final done = await runWithReauth(
        context,
        reason: 'Installing a license needs your own password.',
        action: (pw) async {
          await ref.read(adminRepositoryProvider).installLicense(_license.text, currentPassword: pw);
        },
      );
      if (done && mounted) {
        _license.clear();
        ref.invalidate(licenseProvider);
        showAdminSnack(context, 'License installed.');
      }
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.licenseManage)) {
      return const NoPermissionScreen(title: 'License');
    }
    final async = ref.watch(licenseProvider);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('License')),
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(
            message: adminMsg(e),
            onRetry: () => ref.invalidate(licenseProvider),
          ),
          data: (l) => ListView(
            padding: const EdgeInsets.all(16),
            children: [
              LicenseBanner(license: l),
              Text(
                'Seats ${l.activeUsers} / ${l.maxUsers}',
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 4),
              LinearProgressIndicator(
                value: l.maxUsers == 0 ? 0 : (l.activeUsers / l.maxUsers).clamp(0, 1).toDouble(),
              ),
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                children: [Chip(label: Text(licenseStatusLabel(l.status)))],
              ),
              const Divider(height: 24),
              _row('Licensed to', l.licensedTo ?? '-'),
              _row('License ID', l.licenseId ?? '-'),
              _row('Expires', l.expiresAt == null ? 'Never' : ymd(l.expiresAt!)),
              _row('After expiry', l.onExpiry == 'read_only' ? 'Read-only' : 'No new employees'),
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const SizedBox(width: 110, child: Text('Deployment ID')),
                  Expanded(child: SelectableText(l.deploymentId)),
                  IconButton(
                    tooltip: 'Copy deployment ID',
                    icon: const Icon(Icons.copy),
                    onPressed: () async {
                      await Clipboard.setData(ClipboardData(text: l.deploymentId));
                      if (context.mounted) showAdminSnack(context, 'Deployment ID copied.');
                    },
                  ),
                ],
              ),
              const Divider(height: 32),
              Text('Install a license', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              Form(
                key: _key,
                child: TextFormField(
                  controller: _license,
                  enabled: !_busy,
                  minLines: 3,
                  maxLines: 6,
                  autocorrect: false,
                  enableSuggestions: false,
                  validator: validateLicenseKey,
                  decoration: const InputDecoration(
                    labelText: 'License key',
                    hintText: 'ECL1.…',
                    border: OutlineInputBorder(),
                  ),
                ),
              ),
              const SizedBox(height: 12),
              FilledButton(
                onPressed: _busy ? null : _install,
                child: const Text('Install license'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _row(String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [SizedBox(width: 110, child: Text(label)), Expanded(child: Text(value))],
        ),
      );
}
