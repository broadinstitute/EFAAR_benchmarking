from typing import Optional

import numpy as np
import pandas as pd
import scanpy as sc
import scipy.linalg as linalg
from scvi.model import SCVI
from sklearn.covariance import EllipticEnvelope
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
from sklearn.utils import Bunch

import efaar_benchmarking.constants as cst


def embed_by_scvi_anndata(
    adata, batch_col: str = cst.REPLOGLE_BATCH_COL, n_latent: int = 128, n_hidden: int = 256
) -> np.ndarray:
    """
    Embed the input AnnData object using scVI.

    Args:
        adata (anndata.AnnData): The AnnData object to be embedded.
        batch_col (str): The batch key in the AnnData object. Defaults to REPLOGLE_BATCH_COL.
        n_latent (int): The number of latent dimensions. Defaults to 128.
        n_hidden (int): The number of hidden dimensions. Defaults to 256.

    Returns:
        numpy.ndarray: Embedding of the input data using scVI.
    """
    adata = adata.copy()
    SCVI.setup_anndata(adata, batch_key=batch_col)
    vae = SCVI(adata, n_hidden=n_hidden, n_latent=n_latent)
    vae.train(use_gpu=True)
    return vae.get_latent_representation()


def embed_by_pca_anndata(adata, n_latent: int = 128) -> np.ndarray:
    """
    Embed the input data using principal component analysis (PCA).
    Note that the data is centered by the `pca` function prior to PCA transformation.

    Args:
        adata (AnnData): Annotated data matrix.
        n_latent (int): Number of principal components to use. Defaults to 128.

    Returns:
        numpy.ndarray: Embedding of the input data using PCA.
    """
    adata = adata.copy()
    sc.pp.pca(adata, n_comps=n_latent)
    return adata.obsm["X_pca"]


def centerscale_on_batch(
    features: np.ndarray, metadata: pd.DataFrame = None, batch_col: Optional[str] = None
) -> np.ndarray:
    """
    Center and scale the input features based on the batch information.

    Args:
        features (np.ndarray): Input features to be centered and scaled.
        metadata (pd.DataFrame): Metadata information for the input features.
        batch_col (str): Name of the column in metadata that contains batch information.

    Returns:
        np.ndarray: Centered and scaled features.
    """
    if batch_col is None:
        features = StandardScaler().fit_transform(features)
    else:
        if metadata is None:
            raise ValueError("metadata must be provided if batch_col is not None")
        batches = metadata[batch_col].unique()
        for batch in batches:
            ind = metadata[batch_col] == batch
            features[ind, :] = StandardScaler().fit_transform(features[ind, :])
    return features


def embed_by_pca(
    features: np.ndarray,
    metadata: pd.DataFrame = None,
    variance_or_ncomp=128,
    batch_col: Optional[str] = None,
) -> np.ndarray:
    """
    Embed the whole input data using principal component analysis (PCA).
    Note that we explicitly center & scale the data before calling `PCA`.
    Centering and scaling is done by batch if `batch_col` is not None, and on the whole data otherwise.
    Also note that `PCA` transformer also does mean-centering on the whole data prior to the PCA operation.

    Args:
        features (np.ndarray): Features to transform
        metadata (pd.DataFrame): Metadata. Defaults to None.
        variance_or_ncomp (float, optional): Variance or number of components to keep after PCA.
            Defaults to 128 (n_components). If between 0 and 1, select the number of components such that
            the amount of variance that needs to be explained is greater than the percentage specified.
            If 1, a single component is kept, and if None, all components are kept.
        batch_col (str, optional): Column name for batch information. Defaults to None.
    Returns:
        np.ndarray: Transformed data using PCA.
    """

    features = centerscale_on_batch(features, metadata, batch_col)
    features = PCA(variance_or_ncomp).fit_transform(features)
    return features


def centerscale_on_controls(
    embeddings: np.ndarray,
    metadata: pd.DataFrame,
    pert_col: str,
    control_key: str,
    scale: bool = True,
) -> np.ndarray:
    """
    Center and scale the embeddings by the control perturbation units in the metadata.

    Args:
        embeddings (numpy.ndarray): The embeddings to be aligned.
        metadata (pandas.DataFrame): The metadata containing information about the embeddings.
        pert_col (str, optional): The column in the metadata containing perturbation information.
        control_key (str, optional): The key for non-targeting controls in the metadata.
        scale (bool): Whether to scale the embeddings besides centering. Defaults to True.

    Returns:
        numpy.ndarray: The aligned embeddings.
    """
    control_ind = metadata[pert_col] == control_key
    ss = StandardScaler() if scale else StandardScaler(with_std=False)
    return ss.fit(embeddings[control_ind]).transform(embeddings)


def tvn_on_controls(
    embeddings: np.ndarray,
    metadata: pd.DataFrame,
    pert_col: str,
    control_key: str,
    batch_col_coral: Optional[str] = None,
) -> np.ndarray:
    """
    Apply TVN (Typical Variation Normalization) to the data based on the control perturbation units.
    Note that the data is first centered and scaled based on the control units.

    Args:
        embeddings (np.ndarray): The embeddings to be normalized.
        metadata (pd.DataFrame): The metadata containing information about the samples.
        pert_col (str): The column name in the metadata DataFrame that represents the perturbation labels.
        control_key (str): The control perturbation label.
        batch_col_coral (str, optional): Column name in the metadata DataFrame representing the batch labels
            to be used for CORAL normalization. Defaults to None.

    Returns:
        np.ndarray: The normalized embeddings.
    """
    ctrl_ind = metadata[pert_col] == control_key
    embeddings = centerscale_on_controls(embeddings, metadata, pert_col, control_key)
    embeddings = PCA().fit(embeddings[ctrl_ind]).transform(embeddings)
    embeddings = centerscale_on_controls(embeddings, metadata, pert_col, control_key)
    if batch_col_coral is not None:
        batches = metadata[batch_col_coral].unique()
        for batch in batches:
            batch_ind = metadata[batch_col_coral] == batch
            batch_control_ind = batch_ind & (metadata[pert_col] == control_key)
            source_cov = np.cov(embeddings[batch_control_ind], rowvar=False, ddof=1) + 0.5 * np.eye(embeddings.shape[1])
            source_cov_half_inv = linalg.fractional_matrix_power(source_cov, -0.5)
            embeddings[batch_ind] = np.matmul(embeddings[batch_ind], source_cov_half_inv)
    return embeddings


def aggregate(
    embeddings: np.ndarray,
    metadata: pd.DataFrame,
    pert_col: str,
    control_key: str,
    method="mean",
) -> Bunch[pd.DataFrame, pd.DataFrame]:
    """
    Apply the mean or median aggregation to replicate embeddings for each perturbation.

    Args:
        embeddings (numpy.ndarray): The embeddings to be aggregated.
        metadata (pandas.DataFrame): The metadata containing information about the embeddings.
        pert_col (str, optional): The column in the metadata containing perturbation information.
        control_key (str, optional): The key for non-targeting controls in the metadata.
        method (str, optional): The aggregation method to use. Must be either "mean" or "median".
            Defaults to "mean".

    Returns:
        Bunch: A named tuple containing two pandas DataFrames:
            - 'features': The aggregated embeddings.
            - 'metadata': A DataFrame containing the perturbation labels for each row in 'data'.
    """

    unique_perts = list(np.unique(metadata[pert_col].values))
    unique_perts.remove(control_key)
    final_embeddings = np.zeros((len(unique_perts), embeddings.shape[1]))
    if method == "mean":
        aggr_func = np.mean
    elif method == "median":
        aggr_func = np.median  # type: ignore[assignment]
    else:
        raise ValueError(f"Invalid aggregation method: {method}")
    for i, pert in enumerate(unique_perts):
        idxs = np.where(metadata[pert_col].values == pert)[0]
        final_embeddings[i, :] = aggr_func(embeddings[idxs, :], axis=0)
    return Bunch(features=pd.DataFrame(final_embeddings), metadata=pd.DataFrame.from_dict({pert_col: unique_perts}))


def filter_to_perturbations(
    features: pd.DataFrame, metadata: pd.DataFrame, perts: list[str], pert_col: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Filters the features and metadata dataframes based on a list of perturbations.

    Args:
        features (pd.DataFrame): The features dataframe.
        metadata (pd.DataFrame): The metadata dataframe.
        perts (list[str]): A list of perturbations to filter.
        pert_col (str, optional): The column name in the metadata dataframe that contains the perturbation labels.

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: A tuple containing the filtered features and metadata dataframes.
    """
    indices = metadata[pert_col].isin(perts)
    return features[indices], metadata[indices]


def filter_cell_profiler_features(
    features: pd.DataFrame,
    metadata: pd.DataFrame,
    filter_by_intensity: bool = True,
    filter_by_cell_count: bool = True,
    drop_image_cols: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Filter the given cell profiler features and metadata dataframes based on various criteria.

    Args:
        features (pd.DataFrame): A dataframe containing the features to filter.
        metadata (pd.DataFrame): A dataframe containing the metadata to filter.
        filter_by_intensity (bool, optional): Whether to filter by intensity. Defaults to True.
        filter_by_cell_count (bool, optional): Whether to filter by cell count. Defaults to True.
        drop_image_cols (bool, optional): Whether to drop image columns. Defaults to True.

    Returns:
        Tuple[pd.DataFrame, pd.DataFrame]: A tuple containing the filtered features and metadata dataframes.
    """
    if filter_by_intensity:
        intensity_cols = [col for col in features.columns if "ImageQuality_MeanIntensity" in col]
        if len(intensity_cols) > 0:
            mask = EllipticEnvelope(contamination=0.01, random_state=42).fit_predict(features[intensity_cols]) == 1
            features = features.loc[mask]
            metadata = metadata.loc[mask]
    if filter_by_cell_count:
        mask = np.full(len(features), True)
        for colname in ["Cytoplasm_Number_Object_Number", "Nuclei_Number_Object_Number"]:
            mask = mask & (features[colname] >= 50) & (features[colname] <= 350)
        features = features.loc[mask]
        metadata = metadata.loc[mask]
    if drop_image_cols:
        features = features.drop(columns=[col for col in features.columns if col.startswith("Image_")])

    return features, metadata
