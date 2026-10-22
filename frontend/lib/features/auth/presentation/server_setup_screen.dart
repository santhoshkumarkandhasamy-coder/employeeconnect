import 'dart:typed_data';

import 'package:flutter/foundation.dart' show kReleaseMode;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/server/company.dart';
import '../../../core/server/server_providers.dart';
import '../../../core/server/server_url.dart';
import 'auth_controller.dart';

/// First-run "Server setup": the user types the company server address, the
/// app asks `GET /public/company` and shows the company name for
/// confirmation before saving. Certificate pins stay build-time.
class ServerSetupScreen extends ConsumerStatefulWidget {
  const ServerSetupScreen({super.key, this.release = kReleaseMode});

  /// Release rules (https only). Overridable for tests.
  final bool release;

  @override
  ConsumerState<ServerSetupScreen> createState() => _ServerSetupScreenState();
}

class _ServerSetupScreenState extends ConsumerState<ServerSetupScreen> {
  final _key = GlobalKey<FormState>();
  final _url = TextEditingController();
  bool _busy = false;
  String? _error;
  String? _baseUrl;
  CompanyInfo? _found;
  Uint8List? _logo;

  @override
  void dispose() {
    _url.dispose();
    super.dispose();
  }

  String? _validate(String? v) => normalizeServerInput(v ?? '', release: widget.release) == null
      ? (widget.release
          ? 'Enter a valid https address, for example hr.company.com'
          : 'Enter a valid address, for example hr.company.com')
      : null;

  Future<void> _check() async {
    if (_busy || !(_key.currentState?.validate() ?? false)) return;
    final base = normalizeServerInput(_url.text, release: widget.release)!;
    setState(() {
      _busy = true;
      _error = null;
      _found = null;
    });
    try {
      final c = await ref.read(serverProbeProvider)(base);
      if (!mounted) return;
      setState(() {
        _found = c.company;
        _logo = c.logo;
        _baseUrl = base;
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _confirm() async {
    final c = _found;
    final base = _baseUrl;
    if (c == null || base == null) return;
    setState(() => _busy = true);
    await ref.read(authControllerProvider.notifier).serverChosen(base, c);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final found = _found;
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Form(
                key: _key,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Semantics(
                      header: true,
                      child: Text(
                        'Server setup',
                        textAlign: TextAlign.center,
                        style: theme.textTheme.headlineMedium,
                      ),
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'Enter the server address your company gave you.',
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 24),
                    TextFormField(
                      controller: _url,
                      enabled: !_busy && found == null,
                      keyboardType: TextInputType.url,
                      autocorrect: false,
                      textInputAction: TextInputAction.go,
                      onFieldSubmitted: (_) => _check(),
                      validator: _validate,
                      decoration: const InputDecoration(
                        labelText: 'Server address',
                        hintText: 'hr.company.com',
                        prefixIcon: Icon(Icons.dns_outlined),
                      ),
                    ),
                    if (_error != null) ...[
                      const SizedBox(height: 12),
                      Semantics(
                        liveRegion: true,
                        child: Text(
                          _error!,
                          textAlign: TextAlign.center,
                          style: TextStyle(color: theme.colorScheme.error),
                        ),
                      ),
                    ],
                    const SizedBox(height: 16),
                    if (found == null)
                      FilledButton(
                        onPressed: _busy ? null : _check,
                        child: _busy
                            ? const SizedBox(
                                height: 22,
                                width: 22,
                                child: CircularProgressIndicator(strokeWidth: 2.5),
                              )
                            : const Text('Continue'),
                      )
                    else ...[
                      Card(
                        child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: Column(
                            children: [
                              if (_logo != null) ...[
                                Image.memory(
                                  _logo!,
                                  height: 64,
                                  excludeFromSemantics: true,
                                  errorBuilder: (_, __, ___) => const SizedBox.shrink(),
                                ),
                                const SizedBox(height: 8),
                              ],
                              const Text('Is this your company?'),
                              const SizedBox(height: 8),
                              Text(
                                found.name ?? 'Employee Connect (not named yet)',
                                textAlign: TextAlign.center,
                                style: theme.textTheme.titleLarge,
                              ),
                            ],
                          ),
                        ),
                      ),
                      const SizedBox(height: 12),
                      FilledButton(
                        onPressed: _busy ? null : _confirm,
                        child: const Text('Yes, use this server'),
                      ),
                      TextButton(
                        onPressed: _busy
                            ? null
                            : () => setState(() {
                                  _found = null;
                                  _baseUrl = null;
                                }),
                        child: const Text('No, change address'),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
