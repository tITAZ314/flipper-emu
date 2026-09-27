using Antmicro.Renode.Plugins;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// Renode plugin entry point for the Flipper Zero peripheral models.
    /// </summary>
    /// <remarks>
    /// Renode discovers extensions by statically scanning the assemblies in its
    /// own directory for this attribute (the same way its bundled
    /// <c>SampleCommandPlugin</c>, <c>tracer</c> and <c>Wireshark Plugin</c> are
    /// found), so the models in this assembly have to be published as a plugin
    /// for the type manager to resolve them when a platform file names them.
    ///
    /// The attribute is only valid on a class, and Renode instantiates the class
    /// when the plugin is enabled:
    ///
    /// <code>
    /// TypeManager.Instance.PluginManager.EnablePlugin("FlipperEmu.Peripherals")
    /// </code>
    ///
    /// <c>flipper_emu.runner</c> enables it on start-up, so no change to the
    /// Renode installation or its configuration is required.
    /// </remarks>
    [Plugin(
        Name = "FlipperEmu.Peripherals",
        Description = "Flipper Zero peripheral models for the emulated STM32WB55 (WB55 EXTI, WB55 GPIO port with a real input path, ST7567 display, SPI SD card).",
        Version = "0.1.0",
        Vendor = "flipper-emu")]
    public class FlipperPeripheralsPlugin
    {
        // Instantiated by Renode when the plugin is enabled; enabling it is what
        // makes the peripheral types in this assembly resolvable.
    }
}

