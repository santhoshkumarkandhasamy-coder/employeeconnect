import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/server/branding.dart';
import '../../../core/server/company.dart';
import '../../../core/widgets/state_views.dart';
import '../../auth/presentation/auth_controller.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import '../domain/ops_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

class PickedImage {
  const PickedImage(this.name, this.bytes);
  final String name;
  final Uint8List bytes;
}

/// Chooses a logo image. Behind an interface so tests need no plugin.
abstract class LogoPicker {
  Future<PickedImage?> pick();
}

class PluginLogoPicker implements LogoPicker {
  const PluginLogoPicker();

  @override
  Future<PickedImage?> pick() async {
    try {
      final r = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: const ['png', 'jpg', 'jpeg'],
        withData: true,
      );
      final f = r?.files.single;
      final b = f?.bytes;
      if (f == null || b == null) return null;
      return PickedImage(f.name, b);
    } on Exception {
      return null;
    }
  }
}

final logoPickerProvider = Provider<LogoPicker>((ref) => const PluginLogoPicker());

/// Name field + optional logo, shared by the setup wizard and the company
/// screen. Calls [onSaved] after a successful save.
class _CompanyForm extends ConsumerStatefulWidget {
  const _CompanyForm({
    super.key,
    required this.initialName,
    required this.initialLogo,
    required this.onSaved,
    required this.actionLabel,
  });
  final String initialName;
  final Uint8List? initialLogo;
  final void Function(CompanyAdmin saved) onSaved;
  final String actionLabel;

  @override
  ConsumerState<_CompanyForm> createState() => _CompanyFormState();
}

class _CompanyFormState extends ConsumerState<_CompanyForm> {
  final _key = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.initialName);
  PickedImage? _logo;
  bool _removeLogo = false;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  Future<void> _pick() async {
    final p = await ref.read(logoPickerProvider).pick();
    if (p == null) return;
    final problem = validateLogoFile(p.name, p.bytes.length);
    if (problem != null) {
      setState(() => _error = problem);
      return;
    }
    setState(() {
      _logo = p;
      _removeLogo = false;
      _error = null;
    });
  }

  Future<void> _save() async {
    if (_busy || !(_key.currentState?.validate() ?? false)) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(adminRepositoryProvider);
    try {
      var saved = await repo.saveCompanyName(_name.text);
      final logo = _logo;
      if (logo != null) {
        saved = await repo.uploadCompanyLogo(logo.name, logo.bytes);
      } else if (_removeLogo && widget.initialLogo != null) {
        await repo.deleteCompanyLogo();
        saved = await repo.company();
      }
      ref.invalidate(companyAdminProvider);
      if (mounted) widget.onSaved(saved);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = adminErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final shown = _removeLogo ? null : (_logo?.bytes ?? widget.initialLogo);
    return Form(
      key: _key,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          TextFormField(
            controller: _name,
            enabled: !_busy,
            textCapitalization: TextCapitalization.words,
            maxLength: 100,
            validator: validateCompanyName,
            decoration: const InputDecoration(
              labelText: 'Company name',
              prefixIcon: Icon(Icons.business_outlined),
            ),
          ),
          const SizedBox(height: 8),
          if (shown != null)
            Center(
              child: Image.memory(
                shown,
                height: 96,
                excludeFromSemantics: true,
                errorBuilder: (_, __, ___) => const SizedBox.shrink(),
              ),
            ),
          const SizedBox(height: 8),
          Wrap(
            alignment: WrapAlignment.center,
            spacing: 8,
            children: [
              OutlinedButton.icon(
                onPressed: _busy ? null : _pick,
                icon: const Icon(Icons.image_outlined),
                label: Text(shown == null ? 'Choose logo (optional)' : 'Change logo'),
              ),
              if (shown != null)
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => setState(() {
                            _logo = null;
                            _removeLogo = true;
                          }),
                  child: const Text('Remove logo'),
                ),
            ],
          ),
          const Text(
            'PNG or JPEG, up to 2 MB.',
            textAlign: TextAlign.center,
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            Semantics(
              liveRegion: true,
              child: Text(
                _error!,
                textAlign: TextAlign.center,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _busy ? null : _save,
            child: _busy
                ? const SizedBox(
                    height: 22,
                    width: 22,
                    child: CircularProgressIndicator(strokeWidth: 2.5),
                  )
                : Text(widget.actionLabel),
          ),
        ],
      ),
    );
  }
}

/// Setup wizard body (name + optional logo); [onSaved] gets the saved
/// company. Used by the mobile route and the console.
class CompanySetupView extends StatelessWidget {
  const CompanySetupView({required this.onConfigured, super.key});
  final void Function(CompanyInfo company) onConfigured;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 440),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Semantics(
                header: true,
                child: Text(
                  'Set up your company',
                  textAlign: TextAlign.center,
                  style: theme.textTheme.headlineSmall,
                ),
              ),
              const SizedBox(height: 8),
              const Text(
                'The name and logo are shown on the sign-in screen and the home screen '
                'of every employee.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 24),
              _CompanyForm(
                initialName: '',
                initialLogo: null,
                actionLabel: 'Finish setup',
                onSaved: (c) => onConfigured(
                  CompanyInfo(
                    name: c.name,
                    logoUrl: c.logoUrl,
                    logoVersion: c.logoVersion,
                    configured: true,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// First-login wizard for the first admin (`company.manage` and the company
/// is not configured yet): name, optional logo, done. Cannot be skipped.
class CompanySetupScreen extends ConsumerWidget {
  const CompanySetupScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) => SecureScreen(
        child: Scaffold(
          body: SafeArea(
            child: CompanySetupView(
              onConfigured: (c) =>
                  ref.read(authControllerProvider.notifier).companyConfigured(c),
            ),
          ),
        ),
      );
}

/// Admin > Company (`company.manage`): rename and change the logo.
class CompanyScreen extends ConsumerWidget {
  const CompanyScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.companyManage)) {
      return const NoPermissionScreen(title: 'Company');
    }
    final async = ref.watch(companyAdminProvider);
    final branding = ref.watch(brandingProvider);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Company')),
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(
            message: adminMsg(e),
            onRetry: () => ref.invalidate(companyAdminProvider),
          ),
          data: (c) => ListView(
            padding: const EdgeInsets.all(16),
            children: [
              _CompanyForm(
                key: ValueKey('company-${c.logoVersion}-${c.name}'),
                initialName: c.name ?? '',
                initialLogo: branding.logo,
                actionLabel: 'Save',
                onSaved: (saved) {
                  ref.read(brandingProvider.notifier).update(CompanyInfo(
                        name: saved.name,
                        logoUrl: saved.logoUrl,
                        logoVersion: saved.logoVersion,
                        configured: saved.configured,
                      ),);
                  showAdminSnack(context, 'Company saved.');
                },
              ),
              const SizedBox(height: 24),
              SelectableText('Deployment ID: ${c.deploymentId}'),
            ],
          ),
        ),
      ),
    );
  }
}
