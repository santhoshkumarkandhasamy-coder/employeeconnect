import 'package:flutter/material.dart';

/// Tappable read-only field used for dates and times.
class PickerField extends StatelessWidget {
  const PickerField({
    required this.label,
    required this.value,
    required this.onTap,
    required this.icon,
    super.key,
  });
  final String label;
  final String? value;
  final VoidCallback? onTap;
  final IconData icon;

  @override
  Widget build(BuildContext context) => Semantics(
        button: true,
        label: '$label: ${value ?? 'not set'}',
        excludeSemantics: true,
        onTap: onTap,
        child: InkWell(
          onTap: onTap,
          child: InputDecorator(
            decoration: InputDecoration(
              labelText: label,
              border: const OutlineInputBorder(),
              suffixIcon: Icon(icon),
            ),
            child: Text(value ?? 'Select', overflow: TextOverflow.ellipsis),
          ),
        ),
      );
}

class FormErrorBox extends StatelessWidget {
  const FormErrorBox(this.message, {super.key});
  final String message;

  @override
  Widget build(BuildContext context) {
    final c = Theme.of(context).colorScheme;
    return Semantics(
      liveRegion: true,
      child: Container(
        padding: const EdgeInsets.all(12),
        margin: const EdgeInsets.only(bottom: 12),
        decoration: BoxDecoration(color: c.errorContainer, borderRadius: BorderRadius.circular(8)),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.error_outline, color: c.onErrorContainer),
            const SizedBox(width: 8),
            Expanded(child: Text(message, style: TextStyle(color: c.onErrorContainer))),
          ],
        ),
      ),
    );
  }
}
