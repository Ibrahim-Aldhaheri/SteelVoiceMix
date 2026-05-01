# Sidecar package shipped alongside steelvoicemix in the same COPR
# project. Provides /usr/lib64/ladspa/librnnoise_ladspa.so — the
# RNNoise LADSPA wrapper from werman/noise-suppression-for-voice
# that powers steelvoicemix's Microphone tab "Noise Reduction" and
# "AI Noise Cancellation" features.
#
# Why we ship our own: the plugin isn't packaged by Fedora at all
# (only the underlying `rnnoise` library is). Without this RPM,
# users would have to clone + cmake + install by hand. By shipping
# it in the same COPR as steelvoicemix, `dnf install steelvoicemix`
# pulls it transitively and the mic features just work.
#
# Build flow: clone the upstream tag, run cmake, install the .so.
# No daemon, no service files, no config — pure plugin drop-in.

%global _version 1.10

Name:           noise-suppression-for-voice
Version:        %{_version}
Release:        1%{?dist}
Summary:        RNNoise LADSPA plugin for real-time voice noise suppression

License:        GPL-3.0-or-later
URL:            https://github.com/werman/noise-suppression-for-voice
Source0:        https://github.com/werman/noise-suppression-for-voice/archive/v%{_version}/noise-suppression-for-voice-%{_version}.tar.gz

BuildRequires:  cmake
BuildRequires:  gcc-c++
BuildRequires:  ladspa-devel
# Werman's CMakeLists vendors rnnoise as a submodule — we can't
# rely on the system librnnoise even though Fedora ships one.
# But if `rnnoise-devel` is present, the build picks it up; either
# way works.

%description
The RNNoise LADSPA plugin (librnnoise_ladspa.so) — a real-time
voice-focused noise suppression filter using a recurrent neural
network. Drop into any LADSPA-aware host (PipeWire filter chains,
PulseAudio's module-ladspa-sink, EasyEffects, etc.) for speech
denoising of microphone input.

This package is built primarily as a dependency of steelvoicemix's
Microphone tab AI / Noise Reduction features, but stands alone.

%prep
%autosetup -n noise-suppression-for-voice-%{_version}

%build
%cmake -DCMAKE_BUILD_TYPE=Release
%cmake_build

%install
# Upstream's CMakeLists doesn't have an install target for the
# LADSPA .so — it just builds it. Install manually to the standard
# multilib LADSPA path.
install -Dm755 %{__cmake_builddir}/ladspa/librnnoise_ladspa.so \
    %{buildroot}%{_libdir}/ladspa/librnnoise_ladspa.so

%files
%license LICENSE
%doc README.md
%{_libdir}/ladspa/librnnoise_ladspa.so

%changelog
* Thu May 01 2026 Ibrahim Aldhaheri <aldaheri.ibrahim@gmail.com> - 1.10-1
- Initial COPR build of werman's RNNoise LADSPA plugin.
